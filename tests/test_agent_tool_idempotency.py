from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import aiosqlite

from plugins import agent_tool_access
from plugins.agent_tool_access import AgentToolAuthorization
from plugins.agent_tools import AgentTool, execute_tool, register_tool
from plugins.agent_tools import confirmation, idempotency
from plugins.agent_tools.call_identity import build_idempotency_key
from plugins.agent_tools.confirmation import confirm_pending_confirmation


def tool_definition(name: str) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Idempotency test tool.",
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
    }
    value.update(updates)
    return value


def allow_tools(monkeypatch, *, effective_group_id: str = "group-1") -> None:
    async def authorize(tool_name, arguments, tool_context):
        return AgentToolAuthorization(True, effective_group_id=effective_group_id)

    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", authorize)


def idempotent_tool(name: str, handler, **overrides: object) -> AgentTool:
    values = {
        "name": name,
        "definition": tool_definition(name),
        "handler": handler,
        "side_effect": "write",
        "risk_level": "medium",
        "idempotency_enabled": True,
        "idempotency_ttl": 120,
        "idempotency_lease_timeout": 30,
        "idempotency_temporary_failure_ttl": 10,
    }
    values.update(overrides)
    return AgentTool(**values)


async def execution_status(idempotency_key: str) -> tuple[str, str, dict[str, object]]:
    async with aiosqlite.connect(idempotency.DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT status, failure_class, side_effect_state_json
            FROM agent_tool_executions
            WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        )
        row = await cursor.fetchone()
    assert row is not None
    import json

    return str(row[0]), str(row[1]), json.loads(str(row[2]))


def test_first_execution_is_saved_and_duplicate_returns_cached_result(monkeypatch) -> None:
    calls = 0

    async def handler(args, tool_context):
        nonlocal calls
        calls += 1
        return {"ok": True, "value": args["value"], "message": "done"}

    allow_tools(monkeypatch)
    name = "idempotency_cached_result_test"
    register_tool(idempotent_tool(name, handler))

    async def run():
        first = await execute_tool(name, {"value": "same"}, context())
        second = await execute_tool(name, {"value": "same"}, context())
        return first, second

    first, second = asyncio.run(run())

    assert first == second
    assert first["ok"] is True
    assert calls == 1


def test_concurrent_duplicate_returns_already_running(monkeypatch) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(args, tool_context):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"ok": True, "message": "done"}

    allow_tools(monkeypatch)
    name = "idempotency_running_test"
    register_tool(idempotent_tool(name, handler))

    async def run():
        first_task = asyncio.create_task(execute_tool(name, {"value": "same"}, context()))
        await started.wait()
        duplicate = await execute_tool(name, {"value": "same"}, context())
        release.set()
        first = await first_task
        return first, duplicate

    first, duplicate = asyncio.run(run())

    assert first["ok"] is True
    assert duplicate["error"] == "already_running"
    assert duplicate["retryable"] is True
    assert calls == 1


def test_key_ignores_confirmation_credentials_and_binds_scope() -> None:
    base = {
        "tool_name": "set_chime",
        "arguments": {"enabled": True},
        "user_id": "user-1",
        "target_type": "group",
        "target_id": "group-1",
        "effective_group_id": "group-1",
    }

    first = build_idempotency_key(**base)
    same = build_idempotency_key(**dict(base))
    changed_user = build_idempotency_key(**{**base, "user_id": "user-2"})
    changed_scope = build_idempotency_key(**{**base, "target_id": "group-2"})
    changed_group = build_idempotency_key(**{**base, "effective_group_id": "group-2"})

    assert first == same
    assert len(first) == 64
    assert len({first, changed_user, changed_scope, changed_group}) == 4


def test_temporary_failure_is_cached_briefly_then_expires(monkeypatch) -> None:
    clock = datetime(2026, 7, 11, tzinfo=timezone.utc)
    monkeypatch.setattr(idempotency, "now_utc", lambda: clock)
    calls = 0

    async def handler(args, tool_context):
        nonlocal calls
        calls += 1
        return {
            "ok": False,
            "error": "external_unavailable",
            "message": "try later",
        }

    allow_tools(monkeypatch)
    name = "idempotency_temporary_failure_test"
    register_tool(
        idempotent_tool(
            name,
            handler,
            idempotency_temporary_errors=frozenset({"external_unavailable"}),
        )
    )

    async def run():
        nonlocal clock
        first = await execute_tool(name, {"value": "same"}, context())
        cached = await execute_tool(name, {"value": "same"}, context())
        clock += timedelta(seconds=11)
        retried = await execute_tool(name, {"value": "same"}, context())
        return first, cached, retried

    first, cached, retried = asyncio.run(run())

    assert first == cached == retried
    assert calls == 2


def test_permanent_failure_is_cached(monkeypatch) -> None:
    calls = 0

    async def handler(args, tool_context):
        nonlocal calls
        calls += 1
        return {"ok": False, "error": "business_denied", "message": "denied"}

    allow_tools(monkeypatch)
    name = "idempotency_permanent_failure_test"
    register_tool(idempotent_tool(name, handler))

    async def run():
        first = await execute_tool(name, {"value": "same"}, context())
        second = await execute_tool(name, {"value": "same"}, context())
        binding = idempotency.create_execution_binding(
            tool_name=name,
            arguments={"value": "same"},
            user_id="user-1",
            target_type="group",
            target_id="group-1",
            effective_group_id="group-1",
        )
        state = await execution_status(binding.idempotency_key)
        return first, second, state

    first, second, state = asyncio.run(run())

    assert first == second
    assert calls == 1
    assert state[0] == idempotency.STATUS_FAILED
    assert state[1] == idempotency.FAILURE_PERMANENT


def test_handler_exception_becomes_unknown_and_is_not_retried(monkeypatch) -> None:
    calls = 0

    async def handler(args, tool_context):
        nonlocal calls
        calls += 1
        raise RuntimeError("execution may have partially completed")

    allow_tools(monkeypatch)
    name = "idempotency_unknown_failure_test"
    register_tool(idempotent_tool(name, handler))

    async def run():
        first = await execute_tool(name, {"value": "same"}, context())
        second = await execute_tool(name, {"value": "same"}, context())
        return first, second

    first, second = asyncio.run(run())

    assert first["error"] == "tool_execution_failed"
    assert second["error"] == "execution_state_unknown"
    assert calls == 1


def test_stale_running_becomes_unknown_not_expired(monkeypatch) -> None:
    clock = datetime(2026, 7, 11, tzinfo=timezone.utc)
    monkeypatch.setattr(idempotency, "now_utc", lambda: clock)
    binding = idempotency.create_execution_binding(
        tool_name="stale_external_action",
        arguments={"value": "same"},
        user_id="user-1",
        target_type="group",
        target_id="group-1",
        effective_group_id="group-1",
    )

    async def run():
        nonlocal clock
        first = await idempotency.claim_execution(binding, ttl_seconds=120, lease_seconds=5)
        clock += timedelta(seconds=6)
        second = await idempotency.claim_execution(binding, ttl_seconds=120, lease_seconds=5)
        state = await execution_status(binding.idempotency_key)
        return first, second, state

    first, second, state = asyncio.run(run())

    assert first.action == "execute"
    assert second.action == "unknown"
    assert state[0] == idempotency.STATUS_UNKNOWN
    assert state[0] != idempotency.STATUS_EXPIRED


def test_distinct_confirmations_share_one_idempotent_execution(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(confirmation, "DB_PATH", tmp_path / "agent_tool_confirmations.db")
    calls = 0

    async def handler(args, tool_context):
        nonlocal calls
        calls += 1
        return {"ok": True, "value": args["value"], "message": "done"}

    allow_tools(monkeypatch)
    name = "idempotency_distinct_confirmations_test"
    register_tool(
        idempotent_tool(
            name,
            handler,
            requires_confirmation=True,
            confirmation_timeout=120,
            risk_level="high",
        )
    )
    tool_context = context()

    async def run():
        first_pending = await execute_tool(name, {"value": "same"}, tool_context)
        second_pending = await execute_tool(name, {"value": "same"}, tool_context)
        first_approval = await confirm_pending_confirmation(
            first_pending["data"]["confirmation_code"],
            user_id="user-1",
            target_type="group",
            target_id="group-1",
        )
        second_approval = await confirm_pending_confirmation(
            second_pending["data"]["confirmation_code"],
            user_id="user-1",
            target_type="group",
            target_id="group-1",
        )
        first = await execute_tool(
            name,
            {"value": "same"},
            {**tool_context, "_tool_confirmation_token": first_approval.token},
        )
        second = await execute_tool(
            name,
            {"value": "same"},
            {**tool_context, "_tool_confirmation_token": second_approval.token},
        )
        return first, second

    first, second = asyncio.run(run())

    assert first == second
    assert calls == 1


def test_read_only_tool_does_not_create_execution_database(monkeypatch) -> None:
    async def handler(args, tool_context):
        return {"ok": True, "value": args["value"]}

    allow_tools(monkeypatch)
    name = "idempotency_read_only_test"
    register_tool(AgentTool(name=name, definition=tool_definition(name), handler=handler))

    result = asyncio.run(execute_tool(name, {"value": "read"}, context()))

    assert result["ok"] is True
    assert idempotency.DB_PATH.exists() is False


def test_external_side_effect_state_keeps_delivery_and_resource_details() -> None:
    render_state = idempotency.side_effect_state(
        "render_semantic_graph",
        {
            "ok": True,
            "data": {
                "graph_id": "graph-1",
                "image_filename": "graph.png",
                "sent": False,
                "send_error": "network unavailable",
            },
            "message": "rendered but not sent",
        },
    )
    reminder_state = idempotency.side_effect_state(
        "create_reminder",
        {
            "ok": True,
            "data": {"id": 42, "remind_at": "2099-01-01 09:00:00"},
            "message": "created",
        },
    )

    assert render_state == {
        "graph_id": "graph-1",
        "image_filename": "graph.png",
        "sent": False,
        "send_error": "network unavailable",
        "result_ok": True,
    }
    assert reminder_state == {
        "id": 42,
        "remind_at": "2099-01-01 09:00:00",
        "result_ok": True,
    }
