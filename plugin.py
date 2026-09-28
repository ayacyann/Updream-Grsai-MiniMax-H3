"""Grsai MiniMax H3 video provider for updream.

This adapter receives updream's internal video request and sends the request
format documented by Grsai:

    POST {base_url}/v1/api/generate
    GET  {base_url}/v1/api/result?id={task_id}

Resolution values supported by the target API are passed through directly:
480p, 768p and 1080p. The adapter also normalizes common updream display
forms such as 768P and maps legacy 720p/2K/1K values to one of those three
quality levels.
"""

from __future__ import annotations

import base64
import logging
import struct
import time
from typing import Any

import requests


SUBMIT_PATH = "/v1/api/generate"
POLL_PATH = "/v1/api/result"
# Keep this list intentionally small: it is the quality contract exposed by
# this plugin. Legacy values are accepted by _normalize_resolution below, but
# never sent to the API.
SUPPORTED_RESOLUTIONS = ("480p", "768p", "1080p")
_RESOLUTION_ALIASES = {
    "720p": "768p",
    "2k": "1080p",
    "1k": "1080p",
}
POLL_INTERVAL_SECONDS = 5.0
POLL_TIMEOUT_SECONDS = 1740.0

logger = logging.getLogger("updream-dc")


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item]
    return []


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _normalize_resolution(value: Any) -> str:
    raw = str(value or "768p").strip().lower()
    if raw in _RESOLUTION_ALIASES:
        return _RESOLUTION_ALIASES[raw]
    if raw in SUPPORTED_RESOLUTIONS:
        return raw
    return "768p"


def _ratio_from_text(value: Any) -> str | None:
    raw = str(value or "").strip().lower()
    if raw in {
        "portrait",
        "vertical",
        "竖屏",
        "9:16",
        "3:4",
        "2:3",
        "4:5",
    }:
        return "portrait"
    if raw in {
        "landscape",
        "horizontal",
        "横屏",
        "16:9",
        "4:3",
        "3:2",
        "5:4",
        "21:9",
        "1:1",
    }:
        return "landscape"
    return None


def _image_bytes_head(source: str, limit: int = 262144) -> bytes:
    if source.startswith("data:"):
        try:
            payload = source.split(",", 1)[1]
            return base64.b64decode(payload[: limit * 2])[:limit]
        except Exception:
            return b""

    try:
        with requests.get(
            source,
            stream=True,
            timeout=(10, 30),
            headers={"User-Agent": "updream-grsai-plugin/1.1"},
        ) as response:
            response.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(32768):
                if not chunk:
                    continue
                chunks.append(chunk)
                size += len(chunk)
                if size >= limit:
                    break
            return b"".join(chunks)[:limit]
    except Exception:
        return b""


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        width, height = struct.unpack(">II", data[16:24])
        return int(width), int(height)
    return None


def _gif_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 10 and data[:6] in {b"GIF87a", b"GIF89a"}:
        width, height = struct.unpack("<HH", data[6:10])
        return int(width), int(height)
    return None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None

    offset = 2
    while offset + 9 < len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9}:
            continue
        if offset + 2 > len(data):
            return None
        segment_length = struct.unpack(">H", data[offset : offset + 2])[0]
        if segment_length < 2 or offset + segment_length > len(data):
            return None
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            height, width = struct.unpack(">HH", data[offset + 3 : offset + 7])
            return int(width), int(height)
        offset += segment_length
    return None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None

    chunk = data[12:16]
    if chunk == b"VP8X":
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if chunk == b"VP8 " and len(data) >= 30:
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    return None


def _image_dimensions(source: str) -> tuple[int, int] | None:
    if not source:
        return None
    head = _image_bytes_head(source)
    for parser in (
        _png_dimensions,
        _jpeg_dimensions,
        _gif_dimensions,
        _webp_dimensions,
    ):
        dimensions = parser(head)
        if dimensions:
            return dimensions
    return None


def _resolve_aspect_ratio(params: dict[str, Any], images: list[str]) -> str:
    raw_ratio = str(
        params.get("aspect_ratio") or params.get("aspectRatio") or ""
    ).strip().lower()
    if raw_ratio in {"portrait", "vertical", "竖屏"}:
        return "portrait"
    if raw_ratio in {"landscape", "horizontal", "横屏"}:
        return "landscape"

    if images:
        dimensions = _image_dimensions(images[0])
        if dimensions:
            width, height = dimensions
            return "landscape" if width >= height else "portrait"

    return _ratio_from_text(raw_ratio) or "landscape"


def _base_urls(params: dict[str, Any]) -> tuple[str, str]:
    config = params.get("config") or {}
    base_url = str(config.get("base_url") or "").rstrip("/")
    if base_url.endswith(SUBMIT_PATH):
        root = base_url[: -len(SUBMIT_PATH)]
    else:
        root = base_url
    root = root.rstrip("/")
    return root + SUBMIT_PATH, root + POLL_PATH


def _build_payload(params: dict[str, Any]) -> dict[str, Any]:
    model = params.get("endpoint_id") or params.get("model") or "minimax-h3"
    model = str(model)
    if model.startswith(("v1:", "v2:")):
        model = model[3:]
    if model.lower() != "minimax-h3":
        model = "minimax-h3"
    images = _dedupe(_as_list(params.get("reference_images")))[:9]
    audios = _dedupe(
        _as_list(
            params.get("audios")
            or params.get("reference_audios")
            or params.get("audio_urls")
        )
    )[:3]

    try:
        duration = int(params.get("duration") or 5)
    except (TypeError, ValueError):
        duration = 5
    duration = max(1, min(15, duration))

    resolution = _normalize_resolution(
        params.get("size")
        or params.get("resolution")
        or params.get("quality")
        or params.get("video_quality")
    )
    if resolution == "1080p":
        duration = min(10, duration)

    payload: dict[str, Any] = {
        "model": model,
        "prompt": str(params.get("prompt") or ""),
        "aspectRatio": _resolve_aspect_ratio(params, images),
        "resolution": resolution,
        "duration": duration,
        "replyType": "async",
    }
    if images:
        payload["images"] = images
    if audios:
        payload["audios"] = audios
    return payload


def _response_data(response: requests.Response) -> dict[str, Any]:
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected response: {data!r}")
    nested = data.get("data")
    if isinstance(nested, dict):
        return nested
    return data


def _video_urls(data: dict[str, Any]) -> list[str]:
    results = data.get("results") or []
    if not isinstance(results, list):
        return []
    urls: list[str] = []
    for item in results:
        if isinstance(item, str) and item:
            urls.append(item)
        elif isinstance(item, dict) and item.get("url"):
            urls.append(str(item["url"]))
    return urls


def _headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _failure(message: str) -> dict[str, Any]:
    return {"status": "failed", "error": str(message)}


def _success(videos: list[str], usage: Any = None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "success", "videos": videos}
    if isinstance(usage, dict):
        result["usage"] = usage
    return result


def generate_video(params: dict[str, Any]) -> dict[str, Any]:
    """Generate one video through Grsai and return the updream plugin format."""

    config = params.get("config") or {}
    api_key = str(config.get("api_key") or "")
    if not api_key:
        return _failure("Missing API key in plugin config")

    payload = _build_payload(params)
    submit_url, poll_url = _base_urls(params)
    headers = _headers(api_key)

    try:
        with requests.Session() as client:
            response = client.post(
                submit_url,
                headers=headers,
                json=payload,
                timeout=(15, 120),
            )
            data = _response_data(response)
            logger.info(
                "[GrsaiPlugin] Submit | resolution=%s ratio=%s duration=%s "
                "images=%s status=%s",
                payload.get("resolution"),
                payload.get("aspectRatio"),
                payload.get("duration"),
                len(payload.get("images") or []),
                data.get("status"),
            )

            urls = _video_urls(data)
            if urls:
                return _success(urls, data.get("usage"))

            status = str(data.get("status") or "").lower()
            if status in {"failed", "violation"}:
                return _failure(data.get("error") or f"Generation {status}")

            task_id = data.get("id") or data.get("task_id")
            if not task_id:
                return _failure(f"Submit response has no task id: {data!r}")
            logger.info("[GrsaiPlugin] Task | id=%s", task_id)

            deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
            polls = 0
            while time.monotonic() < deadline:
                time.sleep(POLL_INTERVAL_SECONDS)
                polls += 1
                poll_response = client.get(
                    poll_url,
                    headers=headers,
                    params={"id": str(task_id)},
                    timeout=(15, 60),
                )
                poll_data = _response_data(poll_response)

                urls = _video_urls(poll_data)
                if urls:
                    return _success(urls, poll_data.get("usage"))

                status = str(poll_data.get("status") or "").lower()
                if polls == 1 or polls % 6 == 0 or status not in {
                    "running",
                    "queued",
                    "processing",
                    "pending",
                    "",
                }:
                    logger.info(
                        "[GrsaiPlugin] Poll | id=%s status=%s progress=%s",
                        task_id,
                        status or "unknown",
                        poll_data.get("progress"),
                    )
                if status in {"failed", "violation"}:
                    return _failure(
                        poll_data.get("error") or f"Generation {status}"
                    )
                if status not in {"running", "queued", "processing", "pending", ""}:
                    return _failure(f"Unknown task status: {status!r}")

            logger.error("[GrsaiPlugin] Poll timeout | id=%s", task_id)
            return _failure(f"Polling timed out after {POLL_TIMEOUT_SECONDS:g}s")
    except Exception as exc:
        logger.exception("[GrsaiPlugin] Error")
        return _failure(str(exc))


__all__ = ["generate_video"]
