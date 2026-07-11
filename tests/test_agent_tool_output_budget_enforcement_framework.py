from __future__ import annotations

import asyncio

import pytest

from plugins import agent_tool_access
from plugins.agent_tool_access import AgentToolAuthorization
from plugins.agent_tools import gateway, idempotency
from plugins.agent_tools.authorization_shadow import MetadataAuthorizationDecision
from plugins.agent_tools.contracts import tool_success
from plugins.agent_tools.output_budget import (
    OUTPUT_BUDGET_ENFORCEMENT_ENV,
    output_budget_enforcement_enabled,
)
from plugins.agent_tools.registry import AgentTool, AgentToolMetadata


def _definition(name: str) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Output budget enforcement framework test tool.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    }


def _tool(name: str, handler, metadata: AgentToolMetadata, **updates: object) -> AgentTool:
    return AgentTool(
        name=name,
        definition=_definition(name),
        handler=handler,
        metadata=metadata,
        **updates,
    )


def _context() -> dict[str, object]:
    return {
        "_target_type": "private",
        "_target_id": "framework-user",
        "_user_id": "framework-user",
        "_tool_call_id": "framework-call",
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


class _Reducer:
    def __init__(self, *, replacement: str = "short", fail: bool = False) -> None:
        self.replacement = replacement
        self.fail = fail
        self.calls = []

    async def reduce(self, result, budget_bytes):
        self.calls.append((result, budget_bytes))
        if self.fail:
            raise RuntimeError("reducer internal output must not leak")
        reduced = dict(result)
        reduced["data"] = dict(result.get("data") or {})
        reduced["data"]["text"] = self.replacement
        return reduced


def test_feature_flag_defaults_disabled(monkeypatch) -> None:
    monkeypatch.delenv(OUTPUT_BUDGET_ENFORCEMENT_ENV, raising=False)
    assert output_budget_enforcement_enabled() is False
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv(OUTPUT_BUDGET_ENFORCEMENT_ENV, value)
        assert output_budget_enforcement_enabled() is True


def test_disabled_flag_keeps_b0_measurement_only(monkeypatch) -> None:
    reducer = _Reducer()
    measurements = []

    async def handler(arguments, context):
        return {"ok": True, "text": "original-output" * 20}

    tool = _tool("framework_disabled", handler, AgentToolMetadata(output_budget=1))
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    monkeypatch.setattr(gateway, "OUTPUT_BUDGET_REDUCERS", {tool.name: reducer})
    monkeypatch.setattr(gateway, "output_budget_enforcement_enabled", lambda: False)
    monkeypatch.setattr(
        gateway,
        "_record_output_budget_shadow",
        lambda **values: measurements.append(values),
    )
    _allow_authorization(monkeypatch)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    assert result["data"]["text"] == "original-output" * 20
    assert reducer.calls == []
    assert measurements[0]["exceeded"] is True


def test_enabled_without_reducer_keeps_result_and_records_missing(monkeypatch) -> None:
    framework_events = []

    async def handler(arguments, context):
        return {"ok": True, "text": "original-output"}

    tool = _tool("framework_missing", handler, AgentToolMetadata(output_budget=1))
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    monkeypatch.setattr(gateway, "OUTPUT_BUDGET_REDUCERS", {})
    monkeypatch.setattr(gateway, "output_budget_enforcement_enabled", lambda: True)
    monkeypatch.setattr(
        gateway,
        "_record_output_budget_framework",
        lambda **values: framework_events.append(values),
    )
    _allow_authorization(monkeypatch)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    assert result["data"]["text"] == "original-output"
    assert framework_events[0]["status"] == "reducer_missing"
    assert framework_events[0]["before_size_bytes"] == framework_events[0]["after_size_bytes"]


def test_successful_reducer_receives_normalized_result_and_is_renormalized(monkeypatch) -> None:
    reducer = _Reducer(replacement="reduced")

    async def handler(arguments, context):
        return {"ok": True, "text": "large-output" * 20}

    tool = _tool("framework_reduced", handler, AgentToolMetadata(output_budget=1))
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    monkeypatch.setattr(gateway, "OUTPUT_BUDGET_REDUCERS", {tool.name: reducer})
    monkeypatch.setattr(gateway, "output_budget_enforcement_enabled", lambda: True)
    _allow_authorization(monkeypatch)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    received, budget = reducer.calls[0]
    assert received["ok"] is True
    assert received["data"]["text"] == "large-output" * 20
    assert received["message"] == ""
    assert budget == 1
    assert result == tool_success({"text": "reduced"})
    assert "data" not in result["data"]


def test_reducer_exception_falls_back_to_original_result(monkeypatch) -> None:
    reducer = _Reducer(fail=True)
    framework_events = []
    original = "original-safe-result"

    async def handler(arguments, context):
        return {"ok": True, "text": original}

    tool = _tool("framework_reducer_failure", handler, AgentToolMetadata(output_budget=1))
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    monkeypatch.setattr(gateway, "OUTPUT_BUDGET_REDUCERS", {tool.name: reducer})
    monkeypatch.setattr(gateway, "output_budget_enforcement_enabled", lambda: True)
    monkeypatch.setattr(
        gateway,
        "_record_output_budget_framework",
        lambda **values: framework_events.append(values),
    )
    _allow_authorization(monkeypatch)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    assert result["data"]["text"] == original
    assert framework_events[0]["status"] == "reducer_failed"


def test_none_budget_never_enters_reducer(monkeypatch) -> None:
    reducer = _Reducer()

    async def handler(arguments, context):
        return {"ok": True, "text": "unchanged"}

    tool = _tool("framework_none", handler, AgentToolMetadata())
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    monkeypatch.setattr(gateway, "OUTPUT_BUDGET_REDUCERS", {tool.name: reducer})
    monkeypatch.setattr(gateway, "output_budget_enforcement_enabled", lambda: True)
    _allow_authorization(monkeypatch)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    assert result["data"]["text"] == "unchanged"
    assert reducer.calls == []


def test_reduced_result_is_cached_and_replayed_exactly(
    idempotency_db, monkeypatch
) -> None:
    handler_calls = 0
    reducer = _Reducer(replacement="cached-reduced")

    async def handler(arguments, context):
        nonlocal handler_calls
        handler_calls += 1
        return {"ok": True, "text": "large-cache-output" * 20}

    tool = _tool(
        "framework_idempotency",
        handler,
        AgentToolMetadata(output_budget=1, idempotency_policy="result_cache"),
        idempotency_enabled=False,
        idempotency_ttl=120,
        idempotency_lease_timeout=30,
    )
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    monkeypatch.setattr(gateway, "OUTPUT_BUDGET_REDUCERS", {tool.name: reducer})
    monkeypatch.setattr(gateway, "output_budget_enforcement_enabled", lambda: True)
    _allow_authorization(monkeypatch)

    async def run_twice():
        first = await gateway.execute_tool(tool.name, {}, _context())
        replay = await gateway.execute_tool(tool.name, {}, _context())
        return first, replay

    first, replay = asyncio.run(run_twice())

    assert first == replay == tool_success({"text": "cached-reduced"})
    assert handler_calls == 1
    assert len(reducer.calls) == 1


def test_timeout_result_cannot_be_changed_to_success(monkeypatch) -> None:
    class _UnsafeReducer:
        async def reduce(self, result, budget_bytes):
            return tool_success({"text": "incorrect-success"})

    async def handler(arguments, context):
        await asyncio.sleep(10)
        return {"ok": True}

    tool = _tool(
        "framework_timeout",
        handler,
        AgentToolMetadata(timeout_seconds=1, output_budget=1),
    )
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    monkeypatch.setattr(gateway, "OUTPUT_BUDGET_REDUCERS", {tool.name: _UnsafeReducer()})
    monkeypatch.setattr(gateway, "output_budget_enforcement_enabled", lambda: True)
    _allow_authorization(monkeypatch)

    async def timeout(awaitable, timeout):
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(gateway.asyncio, "wait_for", timeout)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    assert result["ok"] is False
    assert result["error"] == "tool_execution_timeout"
    assert result["retryable"] is False


def test_framework_runs_after_authorization_and_handler(monkeypatch) -> None:
    order: list[str] = []

    class _OrderedReducer:
        async def reduce(self, result, budget_bytes):
            order.append("reducer")
            return result

    async def handler(arguments, context):
        order.append("handler")
        return {"ok": True, "text": "large" * 20}

    tool = _tool("framework_order", handler, AgentToolMetadata(output_budget=1))
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    monkeypatch.setattr(gateway, "OUTPUT_BUDGET_REDUCERS", {tool.name: _OrderedReducer()})
    monkeypatch.setattr(gateway, "output_budget_enforcement_enabled", lambda: True)
    _allow_authorization(monkeypatch, order)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    assert result["ok"] is True
    assert order == ["authorization", "metadata_authorization", "handler", "reducer"]


class _SafeLogger:
    def __init__(self) -> None:
        self.messages = []

    def info(self, message, *args):
        self.messages.append((message, args))

    def warning(self, message, *args):
        self.messages.append((message, args))


def test_framework_logs_do_not_include_result_arguments_or_identity(monkeypatch) -> None:
    secret = "SECRET_RESULT_CONTENT"

    async def handler(arguments, context):
        return {"ok": True, "text": secret}

    tool = _tool("framework_safe_log", handler, AgentToolMetadata(output_budget=1))
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    monkeypatch.setattr(gateway, "OUTPUT_BUDGET_REDUCERS", {})
    monkeypatch.setattr(gateway, "output_budget_enforcement_enabled", lambda: True)
    _allow_authorization(monkeypatch)
    logger = _SafeLogger()
    monkeypatch.setattr(gateway, "logger", logger)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))
    rendered = str(logger.messages)

    assert result["data"]["text"] == secret
    assert secret not in rendered
    assert "framework-user" not in rendered
    assert "arguments" not in rendered
    assert "reducer_missing" in rendered
