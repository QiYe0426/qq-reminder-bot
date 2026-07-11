from __future__ import annotations

import asyncio
from collections import defaultdict

import aiosqlite
import pytest

from plugins import agent_tool_access
from plugins.agent_tool_access import AgentToolAuthorization
from plugins.agent_tools import AgentTool, execute_tool, register_tool
from plugins.agent_tools import audit, confirmation, gateway
from plugins.agent_tools.confirmation import confirm_pending_confirmation


def tool_definition(name: str) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Audit gateway test tool.",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    }


def context(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "_user_id": "user-1",
        "_target_type": "group",
        "_target_id": "group-1",
        "_tool_call_id": "call-1",
        "_invocation_source": "agent",
    }
    value.update(updates)
    return value


def allow_tools(monkeypatch, *, effective_group_id: str = "group-1") -> None:
    async def authorize(tool_name, arguments, tool_context):
        return AgentToolAuthorization(True, effective_group_id=effective_group_id)

    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", authorize)


async def audit_rows() -> list[aiosqlite.Row]:
    async with aiosqlite.connect(audit.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM agent_tool_audit_events ORDER BY occurred_at, invocation_id, sequence"
        )
        return await cursor.fetchall()


def event_groups(rows: list[aiosqlite.Row]) -> list[list[aiosqlite.Row]]:
    grouped: dict[str, list[aiosqlite.Row]] = defaultdict(list)
    for row in rows:
        grouped[str(row["invocation_id"])].append(row)
    return [sorted(items, key=lambda item: int(item["sequence"])) for items in grouped.values()]


@pytest.fixture
def confirmation_db(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(confirmation, "DB_PATH", tmp_path / "agent_tool_confirmations.db")


def test_successful_execution_writes_complete_event_chain(monkeypatch) -> None:
    async def handler(arguments, tool_context):
        return {"ok": True, "value": arguments["value"]}

    allow_tools(monkeypatch)
    name = "audit_gateway_success_test"
    register_tool(AgentTool(name=name, definition=tool_definition(name), handler=handler))

    result = asyncio.run(execute_tool(name, {"value": "safe"}, context()))
    rows = asyncio.run(audit_rows())

    assert result["ok"] is True
    assert [str(row["event_type"]) for row in rows] == [
        "tool_requested",
        "execution_started",
        "execution_completed",
    ]
    assert {str(row["invocation_id"]) for row in rows} == {str(rows[0]["invocation_id"])}
    assert [int(row["sequence"]) for row in rows] == [1, 2, 3]
    assert all(row["arguments_fingerprint"] for row in rows)
    assert rows[-1]["outcome"] == "success"


def test_authorization_denial_is_audited_without_handler(monkeypatch) -> None:
    called = False

    async def handler(arguments, tool_context):
        nonlocal called
        called = True
        return {"ok": True}

    async def deny(tool_name, arguments, tool_context):
        return AgentToolAuthorization(False, error="group_permission_denied", message="denied")

    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", deny)
    name = "audit_gateway_denied_test"
    register_tool(AgentTool(name=name, definition=tool_definition(name), handler=handler))

    result = asyncio.run(execute_tool(name, {"value": "safe"}, context()))
    rows = asyncio.run(audit_rows())

    assert result["error"] == "group_permission_denied"
    assert called is False
    assert [str(row["event_type"]) for row in rows] == ["tool_requested", "authorization_denied"]
    assert rows[-1]["error_code"] == "group_permission_denied"


def test_confirmation_required_and_accepted_are_audited(confirmation_db, monkeypatch) -> None:
    calls = 0

    async def handler(arguments, tool_context):
        nonlocal calls
        calls += 1
        return {"ok": True}

    allow_tools(monkeypatch)
    name = "audit_gateway_confirmation_test"
    register_tool(
        AgentTool(
            name=name,
            definition=tool_definition(name),
            handler=handler,
            side_effect="write",
            risk_level="high",
            requires_confirmation=True,
        )
    )
    tool_context = context()

    async def run():
        pending = await execute_tool(name, {"value": "safe"}, tool_context)
        approval = await confirm_pending_confirmation(
            str(pending["data"]["confirmation_code"]),
            user_id="user-1",
            target_type="group",
            target_id="group-1",
        )
        executed = await execute_tool(
            name,
            {"value": "safe"},
            {**tool_context, "_tool_confirmation_token": approval.token},
        )
        return pending, executed

    pending, executed = asyncio.run(run())
    groups = event_groups(asyncio.run(audit_rows()))

    assert pending["error"] == "confirmation_required"
    assert executed["ok"] is True
    assert calls == 1
    assert any([row["event_type"] for row in group] == ["tool_requested", "confirmation_required"] for group in groups)
    assert any(
        [row["event_type"] for row in group]
        == ["tool_requested", "confirmation_accepted", "execution_started", "execution_completed"]
        for group in groups
    )


def test_confirmation_token_replay_is_audited_and_not_executed_again(confirmation_db, monkeypatch) -> None:
    calls = 0

    async def handler(arguments, tool_context):
        nonlocal calls
        calls += 1
        return {"ok": True}

    allow_tools(monkeypatch)
    name = "audit_gateway_confirmation_replay_test"
    register_tool(
        AgentTool(
            name=name,
            definition=tool_definition(name),
            handler=handler,
            side_effect="write",
            risk_level="high",
            requires_confirmation=True,
        )
    )
    tool_context = context()

    async def run():
        pending = await execute_tool(name, {"value": "safe"}, tool_context)
        approval = await confirm_pending_confirmation(
            str(pending["data"]["confirmation_code"]),
            user_id="user-1",
            target_type="group",
            target_id="group-1",
        )
        approved_context = {**tool_context, "_tool_confirmation_token": approval.token}
        first = await execute_tool(name, {"value": "safe"}, approved_context)
        replay = await execute_tool(name, {"value": "safe"}, approved_context)
        return first, replay

    first, replay = asyncio.run(run())
    groups = event_groups(asyncio.run(audit_rows()))

    assert first["ok"] is True
    assert replay["error"] == "confirmation_consumed"
    assert calls == 1
    assert any(
        group[-1]["event_type"] == "execution_failed"
        and group[-1]["execution_stage"] == "confirmation"
        and group[-1]["error_code"] == "confirmation_consumed"
        for group in groups
    )


def test_idempotency_replay_is_audited_without_second_handler_call(monkeypatch) -> None:
    calls = 0

    async def handler(arguments, tool_context):
        nonlocal calls
        calls += 1
        return {"ok": True, "value": arguments["value"]}

    allow_tools(monkeypatch)
    name = "audit_gateway_idempotency_replay_test"
    register_tool(
        AgentTool(
            name=name,
            definition=tool_definition(name),
            handler=handler,
            side_effect="write",
            risk_level="medium",
            idempotency_enabled=True,
        )
    )

    async def run():
        first = await execute_tool(name, {"value": "safe"}, context())
        second = await execute_tool(name, {"value": "safe"}, context())
        return first, second

    first, second = asyncio.run(run())
    groups = event_groups(asyncio.run(audit_rows()))

    assert first == second
    assert calls == 1
    assert any("idempotency_claimed" in [row["event_type"] for row in group] for group in groups)
    assert any([row["event_type"] for row in group] == ["tool_requested", "idempotency_replayed"] for group in groups)


def test_handler_exception_writes_execution_failed(monkeypatch) -> None:
    async def handler(arguments, tool_context):
        raise RuntimeError("handler secret must not be stored")

    allow_tools(monkeypatch)
    name = "audit_gateway_handler_exception_test"
    register_tool(AgentTool(name=name, definition=tool_definition(name), handler=handler))

    result = asyncio.run(execute_tool(name, {"value": "safe"}, context()))
    rows = asyncio.run(audit_rows())

    assert result["error"] == "tool_execution_failed"
    assert [str(row["event_type"]) for row in rows] == [
        "tool_requested",
        "execution_started",
        "execution_failed",
    ]
    assert rows[-1]["error_code"] == "tool_execution_failed"
    assert rows[-1]["failure_class"] == "unknown"
    assert "handler secret" not in str(dict(rows[-1]))


def test_execution_started_audit_failure_blocks_high_risk_handler(monkeypatch) -> None:
    called = False

    async def handler(arguments, tool_context):
        nonlocal called
        called = True
        return {"ok": True}

    allow_tools(monkeypatch)
    name = "audit_gateway_fail_closed_test"
    register_tool(
        AgentTool(
            name=name,
            definition=tool_definition(name),
            handler=handler,
            side_effect="external",
            risk_level="high",
        )
    )
    original_append = audit.append_event

    async def fail_execution_started(**values):
        if values["event_type"] == "execution_started":
            raise RuntimeError("audit unavailable")
        return await original_append(**values)

    monkeypatch.setattr(gateway.audit, "append_event", fail_execution_started)

    result = asyncio.run(execute_tool(name, {"value": "safe"}, context()))
    rows = asyncio.run(audit_rows())

    assert result["error"] == "audit_unavailable"
    assert result["retryable"] is True
    assert called is False
    assert [str(row["event_type"]) for row in rows] == ["tool_requested", "execution_failed"]
    assert rows[-1]["execution_stage"] == "audit"
