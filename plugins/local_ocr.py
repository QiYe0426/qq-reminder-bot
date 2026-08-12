from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from nonebot.log import logger

from plugins.safe_http_fetch import fetch_public_url


LOCAL_OCR_ENABLED_ENV = "LOCAL_OCR_ENABLED"
LOCAL_OCR_CONFIDENCE_ENV = "LOCAL_OCR_CONFIDENCE"
LOCAL_OCR_TIMEOUT_SECONDS_ENV = "LOCAL_OCR_TIMEOUT_SECONDS"
LOCAL_OCR_MAX_IMAGE_BYTES_ENV = "LOCAL_OCR_MAX_IMAGE_BYTES"
LOCAL_OCR_CACHE_TTL_SECONDS_ENV = "LOCAL_OCR_CACHE_TTL_SECONDS"
LOCAL_OCR_CACHE_MAX_ENTRIES_ENV = "LOCAL_OCR_CACHE_MAX_ENTRIES"
LOCAL_OCR_CONCURRENCY_ENV = "LOCAL_OCR_CONCURRENCY"

DEFAULT_CONFIDENCE = 0.5
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_MAX_IMAGE_BYTES = 5 * 1024 * 1024
DEFAULT_CACHE_TTL_SECONDS = 300
DEFAULT_CACHE_MAX_ENTRIES = 128
DEFAULT_CONCURRENCY = 1
MAX_IMAGE_BYTES_LIMIT = 20 * 1024 * 1024
USER_AGENT = "HunterBot-LocalOCR/1.0"


class LocalOCRError(RuntimeError):
    """Raised when local OCR cannot safely process an image."""


@dataclass(frozen=True)
class _CacheEntry:
    expires_at: float
    text: str


_engine: Any | None = None
_engine_error: BaseException | None = None
_engine_lock = threading.Lock()
_cache: OrderedDict[str, _CacheEntry] = OrderedDict()
_cache_lock = threading.Lock()
_semaphore: asyncio.Semaphore | None = None
_semaphore_loop: asyncio.AbstractEventLoop | None = None
_semaphore_limit: int | None = None


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except ValueError:
        value = default
    return min(max(value, minimum), maximum)


def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)).strip())
    except ValueError:
        value = default
    return min(max(value, minimum), maximum)


def local_ocr_enabled() -> bool:
    return _bool_env(LOCAL_OCR_ENABLED_ENV, True)


def local_ocr_engine_name() -> str:
    return "RapidOCR/ONNXRuntime（本地）"


def confidence_threshold() -> float:
    return _float_env(LOCAL_OCR_CONFIDENCE_ENV, DEFAULT_CONFIDENCE, 0.0, 1.0)


def timeout_seconds() -> int:
    return _int_env(LOCAL_OCR_TIMEOUT_SECONDS_ENV, DEFAULT_TIMEOUT_SECONDS, 1, 60)


def max_image_bytes() -> int:
    return _int_env(LOCAL_OCR_MAX_IMAGE_BYTES_ENV, DEFAULT_MAX_IMAGE_BYTES, 1, MAX_IMAGE_BYTES_LIMIT)


def cache_ttl_seconds() -> int:
    return _int_env(LOCAL_OCR_CACHE_TTL_SECONDS_ENV, DEFAULT_CACHE_TTL_SECONDS, 1, 3600)


def cache_max_entries() -> int:
    return _int_env(LOCAL_OCR_CACHE_MAX_ENTRIES_ENV, DEFAULT_CACHE_MAX_ENTRIES, 1, 1024)


def concurrency_limit() -> int:
    return _int_env(LOCAL_OCR_CONCURRENCY_ENV, DEFAULT_CONCURRENCY, 1, 2)


def _load_engine() -> Any:
    from rapidocr import RapidOCR

    return RapidOCR()


def _get_engine() -> Any:
    global _engine, _engine_error
    if _engine is not None:
        return _engine
    if _engine_error is not None:
        raise LocalOCRError("local OCR unavailable") from _engine_error
    with _engine_lock:
        if _engine is not None:
            return _engine
        if _engine_error is not None:
            raise LocalOCRError("local OCR unavailable") from _engine_error
        try:
            _engine = _load_engine()
        except BaseException as exc:
            _engine_error = exc
            logger.error("Local OCR engine initialization failed: {}", type(exc).__name__)
            raise LocalOCRError("local OCR unavailable") from exc
    return _engine


def _point_coordinate(box: Any, index: int) -> float:
    try:
        values = list(box)
        coordinates = [float(list(point)[index]) for point in values]
    except (TypeError, ValueError, IndexError):
        return 0.0
    return min(coordinates, default=0.0)


def _result_parts(result: Any) -> tuple[list[Any], list[Any], list[Any]]:
    boxes = getattr(result, "boxes", None)
    texts = getattr(result, "txts", None)
    scores = getattr(result, "scores", None)
    if boxes is not None and texts is not None and scores is not None:
        return list(boxes), list(texts), list(scores)

    if isinstance(result, tuple) and result:
        result = result[0]
    if isinstance(result, list):
        parsed_boxes: list[Any] = []
        parsed_texts: list[Any] = []
        parsed_scores: list[Any] = []
        for item in result:
            if not isinstance(item, (list, tuple)) or len(item) < 3:
                continue
            parsed_boxes.append(item[0])
            parsed_texts.append(item[1])
            parsed_scores.append(item[2])
        return parsed_boxes, parsed_texts, parsed_scores
    return [], [], []


def _normalize_result(result: Any) -> str:
    boxes, texts, scores = _result_parts(result)
    lines: list[tuple[float, float, str]] = []
    threshold = confidence_threshold()
    for box, raw_text, raw_score in zip(boxes, texts, scores):
        text = " ".join(str(raw_text or "").split()).strip()
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            continue
        if text and score >= threshold:
            lines.append((_point_coordinate(box, 1), _point_coordinate(box, 0), text))
    lines.sort(key=lambda item: (item[0], item[1]))
    return "\n".join(text for _, _, text in lines)


def _run_inference(body: bytes) -> str:
    return _normalize_result(_get_engine()(body))


async def _run_inference_async(body: bytes) -> str:
    return await asyncio.to_thread(_run_inference, body)


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore, _semaphore_loop, _semaphore_limit
    loop = asyncio.get_running_loop()
    limit = concurrency_limit()
    if _semaphore is None or _semaphore_loop is not loop or _semaphore_limit != limit:
        _semaphore = asyncio.Semaphore(limit)
        _semaphore_loop = loop
        _semaphore_limit = limit
    return _semaphore


def _cache_get(key: str) -> str | None:
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        if entry.expires_at <= now:
            del _cache[key]
            return None
        _cache.move_to_end(key)
        return entry.text


def _cache_put(key: str, text: str) -> None:
    with _cache_lock:
        _cache[key] = _CacheEntry(time.monotonic() + cache_ttl_seconds(), text)
        _cache.move_to_end(key)
        while len(_cache) > cache_max_entries():
            _cache.popitem(last=False)


async def extract_text_from_bytes(body: bytes) -> str:
    if not local_ocr_enabled():
        return ""
    if not body:
        raise LocalOCRError("empty image payload")
    if len(body) > max_image_bytes():
        raise LocalOCRError("image size exceeds local OCR limit")

    key = hashlib.sha256(body).hexdigest()
    cached = _cache_get(key)
    if cached is not None:
        return cached

    try:
        async with _get_semaphore():
            cached = _cache_get(key)
            if cached is not None:
                return cached
            text = await asyncio.wait_for(_run_inference_async(body), timeout=timeout_seconds())
    except asyncio.TimeoutError as exc:
        raise LocalOCRError("local OCR timeout") from exc
    except LocalOCRError:
        raise
    except Exception as exc:
        raise LocalOCRError("local OCR inference failed") from exc

    _cache_put(key, text)
    return text


async def extract_text_from_url(url: str) -> str:
    if not local_ocr_enabled():
        return ""
    response = await fetch_public_url(
        url,
        timeout_seconds=timeout_seconds(),
        max_bytes=max_image_bytes(),
        headers={"User-Agent": USER_AGENT},
    )
    if response.content_type and "image" not in response.content_type.lower():
        raise LocalOCRError("URL did not return an image")
    return await extract_text_from_bytes(response.body)


def reset_local_ocr_state_for_tests() -> None:
    global _engine, _engine_error, _semaphore, _semaphore_loop, _semaphore_limit
    _engine = None
    _engine_error = None
    _semaphore = None
    _semaphore_loop = None
    _semaphore_limit = None
    with _cache_lock:
        _cache.clear()
