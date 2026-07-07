import asyncio

from plugins import reminder_service
from plugins.agent_tools import (
    get_agent_tool_definitions,
    has_agent_tool,
    merge_agent_tool_definitions,
    run_registered_agent_tool,
)
from plugins.reminder_service import ReminderScope


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
    assert {"create_reminder", "list_reminders", "cancel_reminder"} <= names


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
            {"reminder_id": created["id"]},
            context,
        )
    )

    assert created["ok"] is True
    assert created["content"] == "drink water"
    assert listed["ok"] is True
    assert listed["reminders"][0]["id"] == created["id"]
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
