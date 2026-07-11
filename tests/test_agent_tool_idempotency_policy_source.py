from __future__ import annotations

import asyncio

import pytest

from plugins import agent_tool_access
from plugins.agent_tool_access import AgentToolAuthorization
from plugins.agent_tools import gateway, idempotency
from plugins.agent_tools.registry import AgentTool, AgentToolMetadata


def _definition(name: str) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Idempotency policy source test tool.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    }


def _context(name: str) -> dict[str, object]:
    return {
        "_user_id": "idempotency-policy-user",
        "_target_type": "group",
        "_target_id": "idempotency-policy-group",
        "_tool_call_id": f"call:{name}",
        "_invocation_source": "agent",
    }


@pytest.fixture
def idempotency_db(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(idempotency, "DB_PATH", tmp_path / "agent_tool_executions.db")


def _allow(monkeypatch) -> None:
    async def authorize(tool_name, arguments, context):
        return AgentToolAuthorization(True, effective_group_id="idempotency-policy-group")

    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", authorize)


def _install_tool(monkeypatch, tool: AgentTool) -> None:
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool if name == tool.name else None)


def test_result_cache_metadata_enables_idempotency_and_keeps_legacy_timing(
    idempotency_db, monkeypatch
) -> None:
    handler_calls = 0
    claim_settings: list[tuple[int, int]] = []
    completion_settings: list[tuple[int, int]] = []

    async def handler(arguments, context):
        nonlocal handler_calls
        handler_calls += 1
        return {"ok": True, "call": handler_calls}

    tool = AgentTool(
        name="idempotency_result_cache_from_metadata",
        definition=_definition("idempotency_result_cache_from_metadata"),
        handler=handler,
        idempotency_enabled=False,
        idempotency_ttl=73,
        idempotency_lease_timeout=19,
        idempotency_temporary_failure_ttl=7,
        metadata=AgentToolMetadata(idempotency_policy="result_cache"),
    )
    _install_tool(monkeypatch, tool)
    _allow(monkeypatch)
    original_claim = gateway.claim_execution
    original_complete = gateway.complete_execution

    async def record_claim(binding, *, ttl_seconds, lease_seconds):
        claim_settings.append((ttl_seconds, lease_seconds))
        return await original_claim(binding, ttl_seconds=ttl_seconds, lease_seconds=lease_seconds)

    async def record_complete(*args, **kwargs):
        completion_settings.append(
            (kwargs["ttl_seconds"], kwargs["temporary_failure_ttl_seconds"])
        )
        return await original_complete(*args, **kwargs)

    monkeypatch.setattr(gateway, "claim_execution", record_claim)
    monkeypatch.setattr(gateway, "complete_execution", record_complete)

    async def run_twice():
        first = await gateway.execute_tool(tool.name, {}, _context(tool.name))
        replay = await gateway.execute_tool(tool.name, {}, _context(tool.name))
        return first, replay

    first, replay = asyncio.run(run_twice())

    assert first == replay
    assert handler_calls == 1
    assert claim_settings == [(73, 19), (73, 19)]
    assert completion_settings == [(73, 7)]


@pytest.mark.parametrize(
    ("name", "metadata_policy", "legacy_enabled", "expected_handler_calls"),
    [
        ("idempotency_none_disabled", "none", False, 2),
        ("idempotency_none_keeps_legacy", "none", True, 1),
        ("idempotency_single_flight_disabled_fallback", "single_flight", False, 2),
        ("idempotency_single_flight_enabled_fallback", "single_flight", True, 1),
    ],
)
def test_idempotency_policy_resolution_cases(
    idempotency_db,
    monkeypatch,
    name: str,
    metadata_policy: str,
    legacy_enabled: bool,
    expected_handler_calls: int,
) -> None:
    handler_calls = 0

    async def handler(arguments, context):
        nonlocal handler_calls
        handler_calls += 1
        return {"ok": True, "call": handler_calls}

    tool = AgentTool(
        name=name,
        definition=_definition(name),
        handler=handler,
        idempotency_enabled=legacy_enabled,
        metadata=AgentToolMetadata(idempotency_policy=metadata_policy),
    )
    _install_tool(monkeypatch, tool)
    _allow(monkeypatch)

    async def run_twice():
        first = await gateway.execute_tool(tool.name, {}, _context(tool.name))
        second = await gateway.execute_tool(tool.name, {}, _context(tool.name))
        return first, second

    first, second = asyncio.run(run_twice())

    assert handler_calls == expected_handler_calls
    if expected_handler_calls == 1:
        assert first == second
    else:
        assert first != second


def test_running_and_already_running_behavior_is_unchanged(idempotency_db, monkeypatch) -> None:
    handler_calls = 0

    async def run_concurrently():
        nonlocal handler_calls
        started = asyncio.Event()
        release = asyncio.Event()

        async def handler(arguments, context):
            nonlocal handler_calls
            handler_calls += 1
            started.set()
            await release.wait()
            return {"ok": True}

        tool = AgentTool(
            name="idempotency_running_from_metadata",
            definition=_definition("idempotency_running_from_metadata"),
            handler=handler,
            idempotency_enabled=False,
            idempotency_lease_timeout=60,
            metadata=AgentToolMetadata(idempotency_policy="result_cache"),
        )
        _install_tool(monkeypatch, tool)
        _allow(monkeypatch)

        first_task = asyncio.create_task(
            gateway.execute_tool(tool.name, {}, _context(tool.name))
        )
        await started.wait()
        second = await gateway.execute_tool(tool.name, {}, _context(tool.name))
        release.set()
        first = await first_task
        return first, second

    first, second = asyncio.run(run_concurrently())

    assert first["ok"] is True
    assert second["error"] == "already_running"
    assert second["retryable"] is True
    assert handler_calls == 1


def test_unknown_state_still_blocks_reexecution(idempotency_db, monkeypatch) -> None:
    handler_calls = 0

    async def handler(arguments, context):
        nonlocal handler_calls
        handler_calls += 1
        raise RuntimeError("side effect outcome unknown")

    tool = AgentTool(
        name="idempotency_unknown_from_metadata",
        definition=_definition("idempotency_unknown_from_metadata"),
        handler=handler,
        idempotency_enabled=False,
        metadata=AgentToolMetadata(idempotency_policy="result_cache"),
    )
    _install_tool(monkeypatch, tool)
    _allow(monkeypatch)

    async def run_twice():
        first = await gateway.execute_tool(tool.name, {}, _context(tool.name))
        second = await gateway.execute_tool(tool.name, {}, _context(tool.name))
        return first, second

    first, second = asyncio.run(run_twice())

    assert first["error"] == "tool_execution_failed"
    assert second["error"] == "execution_state_unknown"
    assert handler_calls == 1
