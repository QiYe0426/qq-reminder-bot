from __future__ import annotations

import asyncio
import inspect

import nonebot
import pytest


try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()


def test_analyze_image_uses_shared_local_ocr(monkeypatch) -> None:
    from plugins import media_insights

    seen: list[str] = []

    async def fake_extract(url: str) -> str:
        seen.append(url)
        return "你好\nOCR text"

    monkeypatch.setattr(media_insights, "extract_local_ocr_text", fake_extract)

    result = asyncio.run(media_insights.analyze_image_url("https://example.com/image.png"))

    assert result == "你好\nOCR text"
    assert seen == ["https://example.com/image.png"]
    assert "AsyncOpenAI" not in inspect.getsource(media_insights.analyze_image_url)


def test_analyze_image_reports_no_detected_text(monkeypatch) -> None:
    from plugins import media_insights

    async def fake_extract(_url: str) -> str:
        return ""

    monkeypatch.setattr(media_insights, "extract_local_ocr_text", fake_extract)

    with pytest.raises(RuntimeError, match="no text detected"):
        asyncio.run(media_insights.analyze_image_url("https://example.com/blank.png"))


def test_media_status_describes_local_engine_without_api_key(monkeypatch) -> None:
    from plugins import media_insights

    monkeypatch.setattr(media_insights, "local_ocr_enabled", lambda: True)
    monkeypatch.setattr(media_insights, "local_ocr_engine_name", lambda: "RapidOCR/ONNXRuntime（本地）")
    monkeypatch.setattr(media_insights, "init_archive_db", lambda: asyncio.sleep(0))

    class Cursor:
        def __init__(self, row=None, rows=None):
            self.row = row
            self.rows = rows or []

        async def fetchone(self):
            return self.row

        async def fetchall(self):
            return self.rows

    class DB:
        row_factory = None

        async def execute(self, sql: str):
            return Cursor({"count": 0}) if "COUNT(*)" in sql and "GROUP BY" not in sql else Cursor(rows=[])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(media_insights.aiosqlite, "connect", lambda _path: DB())

    status = asyncio.run(media_insights.media_insight_status_text())
    assert "RapidOCR/ONNXRuntime（本地）" in status
    assert "图片密钥" not in status
