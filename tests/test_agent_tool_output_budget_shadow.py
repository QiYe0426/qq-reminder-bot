from __future__ import annotations

import asyncio

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
            "description": "Output budget shadow test tool.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    }


def _tool(name: str, handler, metadata: AgentToolMetadata) -> AgentTool:
    return AgentTool(
        name=name,
        definition=_definition(name),
        handler=handler,
        metadata=metadata,
    )


def _context() -> dict[str, object]:
    return {
        "_target_type": "private",
        "_target_id": "budget-user",
        "_user_id": "budget-user",
        "_tool_call_id": "budget-call",
        "_invocation_source": "agent",
    }


def _allow_authorization(monkeypatch) -> None:
    async def authorize(tool_name, arguments, context):
        return AgentToolAuthorization(True)

    async def metadata(**values):
        return MetadataAuthorizationDecision(True, (), "", (), ())

    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", authorize)
    monkeypatch.setattr(gateway, "evaluate_metadata_authorization", metadata)
    monkeypatch.setattr(gateway, "authorization_v2_enforcement_enabled", lambda: False)
    monkeypatch.setattr(gateway, "output_budget_enforcement_enabled", lambda: False)
    monkeypatch.setattr(gateway, "log_authorization_shadow_comparison", lambda **values: None)


@pytest.fixture
def idempotency_db(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(idempotency, "DB_PATH", tmp_path / "agent_tool_executions.db")


def test_none_budget_does_not_measure_or_change_result(monkeypatch) -> None:
    async def handler(arguments, context):
        return {"ok": True, "text": "unchanged"}

    tool = _tool("output_budget_none", handler, AgentToolMetadata())
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    _allow_authorization(monkeypatch)

    def unexpected(**values):
        raise AssertionError("None output budget must not emit measurement")

    monkeypatch.setattr(gateway, "_record_output_budget_shadow", unexpected)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    assert result["ok"] is True
    assert result["data"]["text"] == "unchanged"


def test_result_inside_budget_is_measured_without_change(monkeypatch) -> None:
    measurements = []

    async def handler(arguments, context):
        return {"ok": True, "text": "small"}

    tool = _tool(
        "output_budget_inside",
        handler,
        AgentToolMetadata(output_budget=1000),
    )
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    _allow_authorization(monkeypatch)
    monkeypatch.setattr(
        gateway,
        "_record_output_budget_shadow",
        lambda **values: measurements.append(values),
    )

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    assert result["data"]["text"] == "small"
    assert measurements[0]["size_bytes"] == gateway._serialized_tool_result_size_bytes(result)
    assert measurements[0]["budget_bytes"] == 1000
    assert measurements[0]["exceeded"] is False


def test_exceeded_budget_only_records_shadow_and_handler_runs_once(monkeypatch) -> None:
    calls = 0
    measurements = []
    output = "sensitive-output-内容" * 20

    async def handler(arguments, context):
        nonlocal calls
        calls += 1
        return {"ok": True, "text": output}

    tool = _tool(
        "output_budget_exceeded",
        handler,
        AgentToolMetadata(output_budget=1),
    )
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    _allow_authorization(monkeypatch)
    monkeypatch.setattr(
        gateway,
        "_record_output_budget_shadow",
        lambda **values: measurements.append(values),
    )

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    assert calls == 1
    assert result["data"]["text"] == output
    assert measurements[0]["exceeded"] is True
    assert set(measurements[0]) == {"tool_name", "size_bytes", "budget_bytes", "exceeded"}


def test_idempotency_replay_is_unchanged_and_not_remeasured(
    idempotency_db, monkeypatch
) -> None:
    calls = 0
    measurements = []

    async def handler(arguments, context):
        nonlocal calls
        calls += 1
        return {"ok": True, "text": "cached-output"}

    tool = AgentTool(
        name="output_budget_replay",
        definition=_definition("output_budget_replay"),
        handler=handler,
        metadata=AgentToolMetadata(
            output_budget=1,
            idempotency_policy="result_cache",
        ),
        idempotency_enabled=False,
        idempotency_ttl=120,
        idempotency_lease_timeout=30,
    )
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    _allow_authorization(monkeypatch)
    monkeypatch.setattr(
        gateway,
        "_record_output_budget_shadow",
        lambda **values: measurements.append(values),
    )

    async def run_twice():
        first = await gateway.execute_tool(tool.name, {}, _context())
        replay = await gateway.execute_tool(tool.name, {}, _context())
        return first, replay

    first, replay = asyncio.run(run_twice())

    assert first == replay
    assert calls == 1
    assert len(measurements) == 1
    assert measurements[0]["exceeded"] is True


class _SafeLogger:
    def __init__(self) -> None:
        self.messages: list[tuple[str, tuple[object, ...]]] = []

    def info(self, message: str, *args: object) -> None:
        self.messages.append((message, args))

    def warning(self, message: str, *args: object) -> None:
        self.messages.append((message, args))


def test_shadow_telemetry_does_not_log_output_content(monkeypatch) -> None:
    secret = "SECRET_OUTPUT_不应进入日志"

    async def handler(arguments, context):
        return {"ok": True, "text": secret}

    tool = _tool(
        "output_budget_safe_log",
        handler,
        AgentToolMetadata(output_budget=1),
    )
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    _allow_authorization(monkeypatch)
    safe_logger = _SafeLogger()
    monkeypatch.setattr(gateway, "logger", safe_logger)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))
    rendered = str(safe_logger.messages)

    assert result["data"]["text"] == secret
    assert secret not in rendered
    assert "output_budget_safe_log" in rendered
    assert "size_bytes" in rendered
    assert "budget_bytes" in rendered


def test_measurement_failure_does_not_affect_runtime(monkeypatch) -> None:
    async def handler(arguments, context):
        return {"ok": True, "text": "still-returned"}

    tool = _tool(
        "output_budget_measurement_failure",
        handler,
        AgentToolMetadata(output_budget=10),
    )
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    _allow_authorization(monkeypatch)

    def fail(result):
        raise RuntimeError("output content must not escape through exception logging")

    monkeypatch.setattr(gateway, "_serialized_tool_result_size_bytes", fail)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    assert result["ok"] is True
    assert result["data"]["text"] == "still-returned"
