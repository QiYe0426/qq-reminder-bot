import asyncio
from datetime import datetime

import aiosqlite

from plugins import access_control, agent_tool_access, knowledge_service, reminder_service
from plugins.agent_tools.admin_tools import mentioned_user_ids
from plugins.agent_tools import group_context_tools
from plugins.agent_tools import (
    get_agent_tool_definitions,
    get_agent_tool,
    has_agent_tool,
    merge_agent_tool_definitions,
    run_registered_agent_tool,
)
from plugins.agent_tools import confirmation
from plugins.agent_tools.confirmation import confirm_pending_confirmation
from plugins.group_context_service import remember_transient_group_message
from plugins.reminder_service import ReminderScope
from plugins.access_control import FEATURE_COLLECTOR, FEATURE_DAILY_REPORT, is_group_feature_enabled


class FakeSegment:
    def __init__(self, segment_type: str, data: dict[str, object]) -> None:
        self.type = segment_type
        self.data = data


class FakeMentionEvent:
    self_id = "999"

    def get_message(self) -> list[FakeSegment]:
        return [
            FakeSegment("at", {"qq": "999"}),
            FakeSegment("text", {"text": " 看画像 "}),
            FakeSegment("at", {"qq": "10001"}),
            FakeSegment("at", {"qq": "10001"}),
        ]


def test_reminder_tools_are_registered() -> None:
    definitions = get_agent_tool_definitions()
    names = {
        item["function"]["name"]
        for item in definitions
        if isinstance(item.get("function"), dict)
    }

    assert has_agent_tool("create_reminder")
    assert has_agent_tool("list_reminders")
    assert has_agent_tool("cancel_reminder")
    assert has_agent_tool("search_sts2_knowledge")
    assert has_agent_tool("get_group_context")
    assert has_agent_tool("build_semantic_graph")
    assert has_agent_tool("get_semantic_graph")
    assert has_agent_tool("render_semantic_graph")
    assert has_agent_tool("set_chime")
    assert {
        "create_reminder",
        "list_reminders",
        "cancel_reminder",
        "search_sts2_knowledge",
        "get_group_context",
        "build_semantic_graph",
        "get_semantic_graph",
        "render_semantic_graph",
        "generate_daily_report",
        "get_group_status",
        "get_group_profile",
        "get_member_profile",
        "set_group_features",
        "set_chime",
    } <= names
    assert get_agent_tool("create_reminder").requires_feature == "ai_chat"
    assert get_agent_tool("search_sts2_knowledge").requires_feature == "ai_chat"
    assert get_agent_tool("get_group_context").requires_group is True
    assert get_agent_tool("build_semantic_graph").requires_feature == FEATURE_COLLECTOR
    assert get_agent_tool("render_semantic_graph").requires_admin is True
    assert get_agent_tool("generate_daily_report").requires_admin is True
    assert get_agent_tool("get_member_profile").requires_group is False
    assert get_agent_tool("set_group_features").requires_admin is True
    for name in {
        "set_group_features",
        "set_chime",
        "build_semantic_graph",
        "render_semantic_graph",
        "generate_daily_report",
    }:
        assert get_agent_tool(name).risk_level == "high"
        assert get_agent_tool(name).requires_confirmation is True


def test_member_profile_tool_can_read_non_bot_mentions() -> None:
    assert mentioned_user_ids({"_event": FakeMentionEvent()}) == ["10001"]


def test_registered_tool_definitions_replace_same_name_base_definition() -> None:
    base = [
        {
            "type": "function",
            "function": {
                "name": "create_reminder",
                "description": "old",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    merged = merge_agent_tool_definitions(base, get_agent_tool_definitions(["create_reminder"]))

    assert len(merged) == 1
    assert merged[0]["function"]["name"] == "create_reminder"
    assert merged[0]["function"]["description"] != "old"


def test_create_list_and_cancel_reminder_through_agent_registry(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(reminder_service, "DB_PATH", tmp_path / "reminders.db")
    monkeypatch.setattr(reminder_service, "_db_ready", False)
    context = {
        "_scope": ReminderScope(user_id="10001", target_type="private"),
        "_user_id": "10001",
    }

    created = asyncio.run(
        run_registered_agent_tool(
            "create_reminder",
            {"text": "2099-01-01 09:00 drink water"},
            context,
        )
    )
    listed = asyncio.run(run_registered_agent_tool("list_reminders", {"limit": 5}, context))
    cancelled = asyncio.run(
        run_registered_agent_tool(
            "cancel_reminder",
            {"reminder_id": created["data"]["id"]},
            context,
        )
    )

    assert created["ok"] is True
    assert created["data"]["content"] == "drink water"
    assert created["data"]["target_user_id"] == "10001"
    assert listed["ok"] is True
    assert listed["data"]["reminders"][0]["id"] == created["data"]["id"]
    assert cancelled["ok"] is True


def test_create_reminder_tool_requires_conversation_scope() -> None:
    result = asyncio.run(
        run_registered_agent_tool(
            "create_reminder",
            {"text": "2099-01-01 09:00 drink water"},
            {},
        )
    )

    assert result["ok"] is False
    assert result["error"] == "missing_event"


def test_create_reminder_tool_rejects_model_controlled_target() -> None:
    result = asyncio.run(
        run_registered_agent_tool(
            "create_reminder",
            {
                "text": "2099-01-01 09:00 drink water",
                "target_user_id": "10002",
                "target_display_name": "other user",
            },
            {
                "_scope": ReminderScope(user_id="10001", target_type="private"),
                "_user_id": "10001",
            },
        )
    )

    assert result["ok"] is False
    assert result["error"] == "invalid_arguments"


def test_create_reminder_tool_rejects_mismatched_identity_scope() -> None:
    result = asyncio.run(
        run_registered_agent_tool(
            "create_reminder",
            {"text": "2099-01-01 09:00 drink water"},
            {
                "_scope": ReminderScope(user_id="10002", target_type="private"),
                "_user_id": "10001",
            },
        )
    )

    assert result["ok"] is False
    assert result["error"] == "invalid_identity_scope"


def test_search_sts2_knowledge_through_agent_registry(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(knowledge_service, "DB_PATH", tmp_path / "companion_memory.db")
    monkeypatch.setattr(knowledge_service, "_knowledge_db_ready", False)

    async def run() -> dict[str, object]:
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
                    "巨像",
                    "巨像是一张 STS2 测试卡牌。",
                    '["巨像"]',
                    "STS2/card",
                    1,
                    timestamp,
                    timestamp,
                ),
            )
            await db.commit()
        return await run_registered_agent_tool("search_sts2_knowledge", {"query": "巨像"}, {})

    result = asyncio.run(run())

    assert result["ok"] is True
    assert result["data"]["count"] == 1
    assert result["data"]["items"][0]["title"] == "巨像"


def test_get_group_context_through_agent_registry(monkeypatch) -> None:
    async def collector_disabled(group_id: str, feature: str) -> bool:
        return False

    monkeypatch.setattr(group_context_tools, "is_group_feature_enabled", collector_disabled)
    group_id = "agent-tool-transient-group"
    remember_transient_group_message(
        group_id=group_id,
        message_id="m1",
        user_id="10001",
        sender_name="群友A",
        plain_text="刚刚说过要喝水",
        segment_types="text",
        created_at="2026-07-07 10:00:00",
    )

    result = asyncio.run(
        run_registered_agent_tool(
            "get_group_context",
            {"keyword": "喝水", "limit": 5},
            {"_target_type": "group", "_target_id": group_id},
        )
    )

    assert result["ok"] is True
    assert result["data"]["source"] == "transient"
    assert result["data"]["count"] == 1
    assert "喝水" in result["data"]["context"]


def test_admin_set_group_features_through_agent_registry(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(access_control, "DB_PATH", tmp_path / "bot_settings.db")
    monkeypatch.setattr(access_control, "_db_ready", False)
    monkeypatch.setattr(agent_tool_access, "ACCESS_DB_PATH", tmp_path / "bot_settings.db")
    monkeypatch.setattr(agent_tool_access, "_db_ready", False)
    monkeypatch.setattr(confirmation, "DB_PATH", tmp_path / "agent_tool_confirmations.db")

    async def allow_target_group(user_id: str, group_id: str, context: dict[str, object]) -> bool:
        return True

    monkeypatch.setattr(agent_tool_access, "target_group_admin_authorized", allow_target_group)

    async def run() -> tuple[dict[str, object], bool, bool]:
        args = {"group_id": "722290838", "collector": True, "daily_report": True}
        context = {"_target_type": "private", "_user_id": "1261957634", "_is_admin": True}
        pending = await run_registered_agent_tool(
            "set_group_features",
            args,
            context,
        )
        approval = await confirm_pending_confirmation(
            pending["data"]["confirmation_code"],
            user_id="1261957634",
            target_type="private",
            target_id="",
        )
        result = await run_registered_agent_tool(
            "set_group_features",
            args,
            {**context, "_tool_confirmation_token": approval.token},
        )
        collector = await is_group_feature_enabled("722290838", FEATURE_COLLECTOR)
        daily_report = await is_group_feature_enabled("722290838", FEATURE_DAILY_REPORT)
        return result, collector, daily_report

    result, collector, daily_report = asyncio.run(run())

    assert result["ok"] is True
    assert collector is True
    assert daily_report is True


def test_admin_set_group_features_rejects_missing_dependency(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(access_control, "DB_PATH", tmp_path / "bot_settings.db")
    monkeypatch.setattr(access_control, "_db_ready", False)
    monkeypatch.setattr(agent_tool_access, "ACCESS_DB_PATH", tmp_path / "bot_settings.db")
    monkeypatch.setattr(agent_tool_access, "_db_ready", False)
    monkeypatch.setattr(confirmation, "DB_PATH", tmp_path / "agent_tool_confirmations.db")

    async def allow_target_group(user_id: str, group_id: str, context: dict[str, object]) -> bool:
        return True

    monkeypatch.setattr(agent_tool_access, "target_group_admin_authorized", allow_target_group)

    async def run() -> dict[str, object]:
        args = {"group_id": "722290838", "daily_report": True}
        context = {"_target_type": "private", "_user_id": "1261957634", "_is_admin": True}
        pending = await run_registered_agent_tool(
            "set_group_features",
            args,
            context,
        )
        approval = await confirm_pending_confirmation(
            pending["data"]["confirmation_code"],
            user_id="1261957634",
            target_type="private",
            target_id="",
        )
        return await run_registered_agent_tool(
            "set_group_features",
            args,
            {**context, "_tool_confirmation_token": approval.token},
        )

    result = asyncio.run(run())

    assert result["ok"] is False
    assert result["error"] == "dependency_failed"
