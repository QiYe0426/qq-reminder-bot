from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest


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
