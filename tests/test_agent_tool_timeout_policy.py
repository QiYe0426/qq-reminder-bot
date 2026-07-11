from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from plugins import agent_tool_access
from plugins.agent_tool_access import AgentToolAuthorization
from plugins.agent_tools import gateway, idempotency
from plugins.agent_tools.authorization_shadow import MetadataAuthorizationDecision
from plugins.agent_tools.registry import AgentTool, AgentToolMetadata


def _definition(name: str) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Handler timeout policy test tool.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    }


def _tool(name: str, handler, *, metadata: AgentToolMetadata | None = None, **updates: object) -> AgentTool:
    return AgentTool(
        name=name,
        definition=_definition(name),
        handler=handler,
        metadata=metadata or AgentToolMetadata(),
        **updates,
    )


def _context() -> dict[str, object]:
    return {
        "_target_type": "private",
        "_target_id": "timeout-user",
        "_user_id": "timeout-user",
        "_tool_call_id": "timeout-call",
        "_invocation_source": "agent",
    }


def _allow_authorization(monkeypatch, order: list[str] | None = None) -> None:
    async def authorize(tool_name, arguments, context):
        if order is not None:
            order.append("authorization")
        return AgentToolAuthorization(True)

    async def metadata(**values):
        if order is not None:
            order.append("metadata_authorization")
        return MetadataAuthorizationDecision(True, (), "", (), ())

    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", authorize)
    monkeypatch.setattr(gateway, "evaluate_metadata_authorization", metadata)
    monkeypatch.setattr(gateway, "authorization_v2_enforcement_enabled", lambda: False)
    monkeypatch.setattr(gateway, "log_authorization_shadow_comparison", lambda **values: None)


@pytest.fixture
def idempotency_db(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(idempotency, "DB_PATH", tmp_path / "agent_tool_executions.db")


def test_no_timeout_preserves_previous_handler_behavior(monkeypatch) -> None:
    calls = 0

    async def handler(arguments, context):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return {"ok": True, "value": "completed"}

    tool = _tool("timeout_none", handler)
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    _allow_authorization(monkeypatch)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    assert result["ok"] is True
    assert result["data"]["value"] == "completed"
    assert calls == 1


def test_handler_completes_inside_timeout(monkeypatch) -> None:
    async def handler(arguments, context):
        await asyncio.sleep(0.01)
        return {"ok": True}

    tool = _tool(
        "timeout_completed",
        handler,
        metadata=AgentToolMetadata(timeout_seconds=1),
    )
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    _allow_authorization(monkeypatch)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    assert result["ok"] is True


def test_timeout_enters_unknown_and_handler_is_not_repeated(
    idempotency_db, monkeypatch
) -> None:
    calls = 0

    async def handler(arguments, context):
        nonlocal calls
        calls += 1
        await asyncio.sleep(10)
        return {"ok": True, "internal": "must not be returned"}

    tool = _tool(
        "timeout_unknown",
        handler,
        metadata=AgentToolMetadata(
            side_effect="external_write",
            risk_level="high",
            timeout_seconds=1,
            idempotency_policy="result_cache",
        ),
        side_effect="external",
        risk_level="high",
        idempotency_enabled=False,
        idempotency_ttl=120,
        idempotency_lease_timeout=30,
    )
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    _allow_authorization(monkeypatch)

    async def run_twice():
        first = await gateway.execute_tool(tool.name, {}, _context())
        second = await gateway.execute_tool(tool.name, {}, _context())
        return first, second

    first, second = asyncio.run(run_twice())

    assert first["error"] == "tool_execution_timeout"
    assert first["retryable"] is False
    assert first["message"] == "工具执行超时，执行结果无法确认。"
    assert "internal" not in str(first)
    assert second["error"] == "execution_state_unknown"
    assert calls == 1


def test_timeout_does_not_change_authorization_order(monkeypatch) -> None:
    order: list[str] = []

    async def handler(arguments, context):
        order.append("handler")
        return {"ok": True}

    tool = _tool(
        "timeout_authorization_order",
        handler,
        metadata=AgentToolMetadata(timeout_seconds=1),
    )
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    _allow_authorization(monkeypatch, order)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    assert result["ok"] is True
    assert order == ["authorization", "metadata_authorization", "handler"]


def test_metadata_timeout_is_not_confirmation_timeout(monkeypatch) -> None:
    captured_timeout = None

    async def handler(arguments, context):
        raise AssertionError("Pending confirmation must not invoke Handler")

    tool = _tool(
        "timeout_confirmation_separation",
        handler,
        metadata=AgentToolMetadata(timeout_seconds=7),
        requires_confirmation=True,
        confirmation_timeout=31,
    )
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    _allow_authorization(monkeypatch)

    async def confirmation(**values):
        nonlocal captured_timeout
        captured_timeout = values["timeout_seconds"]
        return SimpleNamespace(
            confirmation_id=1,
            confirmation_code="ABCDEFGH",
            arguments_hash="hash",
            expires_at="2099-01-01T00:00:00+00:00",
            status="pending",
        )

    monkeypatch.setattr(gateway, "create_pending_confirmation", confirmation)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    assert result["error"] == "confirmation_required"
    assert captured_timeout == 31
