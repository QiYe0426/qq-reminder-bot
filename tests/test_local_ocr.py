from __future__ import annotations

import asyncio
import io
import threading
from dataclasses import dataclass

import pytest
from PIL import Image


@dataclass
class FakeResult:
    boxes: list[list[list[float]]]
    txts: list[str]
    scores: list[float]


class FakeEngine:
    def __init__(self, result: FakeResult) -> None:
        self.result = result
        self.calls = 0

    def __call__(self, _body: bytes) -> FakeResult:
        self.calls += 1
        return self.result


def result_with_lines(*lines: tuple[float, float, str, float]) -> FakeResult:
    return FakeResult(
        boxes=[[[left, top], [left + 20, top], [left + 20, top + 10], [left, top + 10]] for top, left, _, _ in lines],
        txts=[text for _, _, text, _ in lines],
        scores=[score for _, _, _, score in lines],
    )


def test_normalizes_result_in_reading_order_and_filters_confidence(monkeypatch) -> None:
    from plugins import local_ocr

    monkeypatch.setenv("LOCAL_OCR_ENABLED", "1")
    monkeypatch.setenv("LOCAL_OCR_CONFIDENCE", "0.5")
    local_ocr.reset_local_ocr_state_for_tests()
    monkeypatch.setattr(local_ocr, "_validate_image_bytes", lambda _body: None)
    engine = FakeEngine(
        result_with_lines(
            (30, 0, "world", 0.91),
            (10, 0, "你好", 0.98),
            (50, 0, "ignored", 0.2),
        )
    )
    monkeypatch.setattr(local_ocr, "_load_engine", lambda: engine)

    assert asyncio.run(local_ocr.extract_text_from_bytes(b"image")) == "你好\nworld"


def test_same_bytes_use_cached_result(monkeypatch) -> None:
    from plugins import local_ocr

    monkeypatch.setenv("LOCAL_OCR_ENABLED", "1")
    local_ocr.reset_local_ocr_state_for_tests()
    monkeypatch.setattr(local_ocr, "_validate_image_bytes", lambda _body: None)
    engine = FakeEngine(result_with_lines((10, 0, "cached text", 0.99)))
    monkeypatch.setattr(local_ocr, "_load_engine", lambda: engine)

    async def run() -> tuple[str, str]:
        return await local_ocr.extract_text_from_bytes(b"same"), await local_ocr.extract_text_from_bytes(b"same")

    assert asyncio.run(run()) == ("cached text", "cached text")
    assert engine.calls == 1


def test_disabled_ocr_returns_empty_without_loading_engine(monkeypatch) -> None:
    from plugins import local_ocr

    monkeypatch.setenv("LOCAL_OCR_ENABLED", "0")
    local_ocr.reset_local_ocr_state_for_tests()
    monkeypatch.setattr(local_ocr, "_load_engine", lambda: pytest.fail("engine should not load"))

    assert asyncio.run(local_ocr.extract_text_from_bytes(b"image")) == ""


def test_empty_and_oversized_images_are_rejected(monkeypatch) -> None:
    from plugins import local_ocr

    monkeypatch.setenv("LOCAL_OCR_ENABLED", "1")
    monkeypatch.setenv("LOCAL_OCR_MAX_IMAGE_BYTES", "16")
    local_ocr.reset_local_ocr_state_for_tests()

    with pytest.raises(local_ocr.LocalOCRError, match="empty"):
        asyncio.run(local_ocr.extract_text_from_bytes(b""))
    with pytest.raises(local_ocr.LocalOCRError, match="size"):
        asyncio.run(local_ocr.extract_text_from_bytes(b"x" * 17))


def test_url_download_uses_safe_fetch_limits(monkeypatch) -> None:
    from plugins import local_ocr
    from plugins.safe_http_fetch import SafeFetchResponse

    monkeypatch.setenv("LOCAL_OCR_ENABLED", "1")
    monkeypatch.setenv("LOCAL_OCR_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("LOCAL_OCR_MAX_IMAGE_BYTES", "12345")
    local_ocr.reset_local_ocr_state_for_tests()
    monkeypatch.setattr(local_ocr, "_validate_image_bytes", lambda _body: None)
    engine = FakeEngine(result_with_lines((10, 0, "downloaded", 0.99)))
    monkeypatch.setattr(local_ocr, "_load_engine", lambda: engine)
    seen: dict[str, object] = {}

    async def fake_fetch(url: str, **kwargs):
        seen.update(url=url, **kwargs)
        return SafeFetchResponse(url=url, final_url=url, content_type="image/png", body=b"png")

    monkeypatch.setattr(local_ocr, "fetch_public_url", fake_fetch)

    assert asyncio.run(local_ocr.extract_text_from_url("https://example.com/a.png")) == "downloaded"
    assert seen["timeout_seconds"] == 7
    assert seen["max_bytes"] == 12345
    assert "User-Agent" in seen["headers"]


def test_timeout_is_reported_as_local_ocr_error(monkeypatch) -> None:
    from plugins import local_ocr

    monkeypatch.setenv("LOCAL_OCR_ENABLED", "1")
    monkeypatch.setenv("LOCAL_OCR_TIMEOUT_SECONDS", "1")
    local_ocr.reset_local_ocr_state_for_tests()

    async def never_returns(_body: bytes) -> str:
        await asyncio.sleep(10)
        return "late"

    monkeypatch.setattr(local_ocr, "_run_inference_async", never_returns)

    with pytest.raises(local_ocr.LocalOCRError, match="timeout"):
        asyncio.run(local_ocr.extract_text_from_bytes(b"image"))


def test_engine_initialization_failure_is_memoized(monkeypatch) -> None:
    from plugins import local_ocr

    monkeypatch.setenv("LOCAL_OCR_ENABLED", "1")
    local_ocr.reset_local_ocr_state_for_tests()
    monkeypatch.setattr(local_ocr, "_validate_image_bytes", lambda _body: None)
    calls = 0

    def fail_load():
        nonlocal calls
        calls += 1
        raise RuntimeError("missing runtime")

    monkeypatch.setattr(local_ocr, "_load_engine", fail_load)

    for body in (b"one", b"two"):
        with pytest.raises(local_ocr.LocalOCRError, match="unavailable"):
            asyncio.run(local_ocr.extract_text_from_bytes(body))
    assert calls == 1


def test_timed_out_inference_cannot_exceed_worker_limit(monkeypatch) -> None:
    from plugins import local_ocr

    monkeypatch.setenv("LOCAL_OCR_ENABLED", "1")
    monkeypatch.setenv("LOCAL_OCR_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("LOCAL_OCR_CONCURRENCY", "1")
    local_ocr.reset_local_ocr_state_for_tests()
    release = threading.Event()
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    def slow_inference(_body: bytes) -> str:
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            release.wait(3)
            return "late"
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(local_ocr, "_run_inference", slow_inference)

    async def run_two() -> None:
        results = await asyncio.gather(
            local_ocr.extract_text_from_bytes(b"first"),
            local_ocr.extract_text_from_bytes(b"second"),
            return_exceptions=True,
        )
        assert all(isinstance(result, local_ocr.LocalOCRError) for result in results)
        release.set()

    asyncio.run(run_two())
    assert max_active == 1


def test_rejects_non_image_bytes_before_loading_engine(monkeypatch) -> None:
    from plugins import local_ocr

    monkeypatch.setenv("LOCAL_OCR_ENABLED", "1")
    local_ocr.reset_local_ocr_state_for_tests()
    monkeypatch.setattr(local_ocr, "_load_engine", lambda: pytest.fail("invalid image must not reach OCR"))

    with pytest.raises(local_ocr.LocalOCRError, match="valid image"):
        asyncio.run(local_ocr.extract_text_from_bytes(b"not-an-image"))


def test_rejects_image_above_pixel_limit(monkeypatch) -> None:
    from plugins import local_ocr

    monkeypatch.setenv("LOCAL_OCR_ENABLED", "1")
    monkeypatch.setenv("LOCAL_OCR_MAX_PIXELS", "100")
    local_ocr.reset_local_ocr_state_for_tests()
    image = Image.new("L", (11, 10), 255)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    with pytest.raises(local_ocr.LocalOCRError, match="pixel limit"):
        asyncio.run(local_ocr.extract_text_from_bytes(buffer.getvalue()))


def test_rejects_multiframe_images(monkeypatch) -> None:
    from plugins import local_ocr

    monkeypatch.setenv("LOCAL_OCR_ENABLED", "1")
    local_ocr.reset_local_ocr_state_for_tests()
    first = Image.new("RGB", (20, 20), "white")
    second = Image.new("RGB", (20, 20), "black")
    buffer = io.BytesIO()
    first.save(buffer, format="GIF", save_all=True, append_images=[second])

    with pytest.raises(local_ocr.LocalOCRError, match="multi-frame"):
        asyncio.run(local_ocr.extract_text_from_bytes(buffer.getvalue()))


def test_reading_order_groups_slightly_skewed_boxes_into_one_row(monkeypatch) -> None:
    from plugins import local_ocr

    monkeypatch.setenv("LOCAL_OCR_ENABLED", "1")
    local_ocr.reset_local_ocr_state_for_tests()
    monkeypatch.setattr(local_ocr, "_validate_image_bytes", lambda _body: None)
    engine = FakeEngine(
        result_with_lines(
            (9, 100, "right", 0.99),
            (10, 0, "left", 0.99),
            (40, 0, "next", 0.99),
        )
    )
    monkeypatch.setattr(local_ocr, "_load_engine", lambda: engine)

    assert asyncio.run(local_ocr.extract_text_from_bytes(b"skewed")) == "left right\nnext"
