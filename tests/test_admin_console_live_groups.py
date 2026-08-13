import asyncio

import nonebot


nonebot.init()

from plugins import admin_console  # noqa: E402


def test_list_groups_includes_live_groups_without_local_history(monkeypatch) -> None:
    async def fake_archived_groups():
        return [
            {"group_id": "1001", "message_count": 10, "last_message_at": "2026-08-13 01:00:00"},
            {"group_id": "1002", "message_count": 5, "last_message_at": "2026-08-12 01:00:00"},
        ]

    async def fake_configured_group_ids():
        return ["1003"]

    async def fake_live_groups():
        return [
            {"group_id": "1001", "group_name": "一群"},
            {"group_id": "1002", "group_name": "二群"},
            {"group_id": "1003", "group_name": "三群"},
            {"group_id": "1004", "group_name": "四群"},
        ]

    monkeypatch.setattr(admin_console, "archived_groups", fake_archived_groups)
    monkeypatch.setattr(admin_console, "configured_group_ids", fake_configured_group_ids)
    monkeypatch.setattr(admin_console, "live_groups", fake_live_groups, raising=False)

    groups = asyncio.run(admin_console.list_groups())

    assert {item["group_id"] for item in groups} == {"1001", "1002", "1003", "1004"}
    fourth = next(item for item in groups if item["group_id"] == "1004")
    assert fourth["group_name"] == "四群"
    assert fourth["message_count"] == 0
