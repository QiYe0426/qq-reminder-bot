from __future__ import annotations

import asyncio
import inspect

import nonebot


try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

def test_extract_image_text_uses_local_ocr_and_strips_urls(monkeypatch) -> None:
    from plugins import group_reactions

    monkeypatch.setattr(group_reactions, "local_ocr_enabled", lambda: True)

    async def fake_extract(url: str) -> str:
        assert url == "https://example.com/image.png"
        return "关键词 https://secret.example/path 更多文字"

    monkeypatch.setattr(group_reactions, "extract_local_ocr_text", fake_extract)

    result = asyncio.run(group_reactions.extract_image_text("https://example.com/image.png"))

    assert result == "关键词 更多文字"
    assert "AsyncOpenAI" not in inspect.getsource(group_reactions.extract_image_text)


def test_extract_image_text_skips_when_local_ocr_disabled(monkeypatch) -> None:
    from plugins import group_reactions

    monkeypatch.setattr(group_reactions, "local_ocr_enabled", lambda: False)

    async def should_not_run(_url: str) -> str:
        raise AssertionError("disabled OCR must not run")

    monkeypatch.setattr(group_reactions, "extract_local_ocr_text", should_not_run)

    assert asyncio.run(group_reactions.extract_image_text("https://example.com/image.png")) == ""


def test_keyword_retort_state_reports_local_engine(monkeypatch) -> None:
    from plugins import group_reactions

    async def rules(_group_id: str):
        return []

    monkeypatch.setattr(group_reactions, "list_keyword_retort_rules", rules)
    monkeypatch.setattr(group_reactions, "local_ocr_engine_name", lambda: "RapidOCR/ONNXRuntime（本地）")

    state = asyncio.run(group_reactions.keyword_retort_state("123"))
    assert state["image_scan"]["engine"] == "RapidOCR/ONNXRuntime（本地）"
    assert "model" not in state["image_scan"]
