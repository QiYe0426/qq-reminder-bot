import asyncio
from datetime import datetime

import aiosqlite

from plugins import knowledge_service


def test_search_sts2_knowledge_filters_category_and_scores(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(knowledge_service, "DB_PATH", tmp_path / "companion_memory.db")
    monkeypatch.setattr(knowledge_service, "_knowledge_db_ready", False)

    async def run() -> dict[str, object]:
        await knowledge_service.ensure_knowledge_db()
        timestamp = datetime(2026, 7, 7, 10, 30).strftime("%Y-%m-%d %H:%M:%S")
        async with aiosqlite.connect(knowledge_service.DB_PATH) as db:
            await db.executemany(
                """
                INSERT INTO companion_knowledge_items
                    (title, content, keywords, category, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "巨像",
                        "巨像是一张 STS2 卡牌，适合用来测试本地知识库检索。",
                        '["巨像", "colossus"]',
                        "STS2/card",
                        1,
                        timestamp,
                        timestamp,
                    ),
                    (
                        "巨像 - 其他游戏",
                        "这条不是 STS2，工具不应该返回它。",
                        '["巨像"]',
                        "Other/card",
                        1,
                        timestamp,
                        timestamp,
                    ),
                ],
            )
            await db.commit()
        return await knowledge_service.search_knowledge_result("巨像", category_prefix="STS2", limit=5)

    result = asyncio.run(run())

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["items"][0]["title"] == "巨像"
    assert result["items"][0]["category"] == "STS2/card"


def test_knowledge_reply_context_uses_shared_lookup_service(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(knowledge_service, "DB_PATH", tmp_path / "companion_memory.db")
    monkeypatch.setattr(knowledge_service, "_knowledge_db_ready", False)

    async def run() -> str:
        await knowledge_service.ensure_knowledge_db()
        timestamp = datetime(2026, 7, 7, 10, 30).strftime("%Y-%m-%d %H:%M:%S")
        async with aiosqlite.connect(knowledge_service.DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO companion_knowledge_items
                    (title, content, keywords, category, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "药水腰带",
                    "药水腰带是一条用于测试预加载上下文的 STS2 知识。",
                    '["药水腰带"]',
                    "STS2/relic",
                    1,
                    timestamp,
                    timestamp,
                ),
            )
            await db.commit()
        return await knowledge_service.knowledge_reply_context("药水腰带怎么样")

    context = asyncio.run(run())

    assert "本地知识库" in context
    assert "药水腰带" in context
