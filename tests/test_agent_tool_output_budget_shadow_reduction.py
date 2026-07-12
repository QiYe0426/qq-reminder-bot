from __future__ import annotations

import asyncio

import pytest

from plugins import agent_tool_access
from plugins.agent_tool_access import AgentToolAuthorization
from plugins.agent_tools import gateway, idempotency
from plugins.agent_tools.authorization_shadow import MetadataAuthorizationDecision
from plugins.agent_tools.contracts import tool_success
from plugins.agent_tools.output_budget import OUTPUT_BUDGET_ENFORCEMENT_ENV
from plugins.agent_tools.output_budget_shadow_reduction import (
    OUTPUT_BUDGET_SHADOW_REDUCERS,
    evaluate_output_budget_shadow_reduction,
)
from plugins.agent_tools.output_reducer_inventory import OUTPUT_REDUCER_INVENTORY
from plugins.agent_tools.registry import AgentTool, AgentToolMetadata
from plugins.agent_tools.reducers.output_budget_text import TextReducer


def _tool(name: str, handler, *, budget: int = 100, idempotent: bool = False) -> AgentTool:
    return AgentTool(
        name=name,
        definition={
            "type": "function",
            "function": {
                "name": name,
                "description": "Shadow reduction test tool.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        },
        handler=handler,
        metadata=AgentToolMetadata(
            output_budget=budget,
            idempotency_policy="result_cache" if idempotent else "none",
        ),
        idempotency_enabled=False,
        idempotency_ttl=120,
        idempotency_lease_timeout=30,
    )


def _context() -> dict[str, object]:
    return {
        "_target_type": "private",
        "_target_id": "shadow-user",
        "_user_id": "shadow-user",
        "_tool_call_id": "shadow-call",
        "_invocation_source": "agent",
    }


def _allow(monkeypatch) -> None:
    async def authorize(tool_name, arguments, context):
        return AgentToolAuthorization(True)

    async def metadata(**values):
        return MetadataAuthorizationDecision(True, (), "", (), ())

    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", authorize)
    monkeypatch.setattr(gateway, "evaluate_metadata_authorization", metadata)
    monkeypatch.setattr(gateway, "authorization_v2_enforcement_enabled", lambda: False)
    monkeypatch.setattr(gateway, "log_authorization_shadow_comparison", lambda **values: None)


@pytest.fixture
def idempotency_db(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(idempotency, "DB_PATH", tmp_path / "shadow-reduction.db")


def test_shadow_does_not_change_return_and_handler_runs_once(monkeypatch) -> None:
    calls = 0
    events = []

    async def handler(arguments, context):
        nonlocal calls
        calls += 1
        return {"ok": True, "summary": "长文本🙂" * 100, "graph_id": "protected"}

    tool = _tool("get_semantic_graph", handler)
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    monkeypatch.setattr(
        gateway,
        "_record_output_budget_shadow_reduction",
        lambda **values: events.append(values),
    )
    _allow(monkeypatch)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    assert result == tool_success(
        {"summary": "长文本🙂" * 100, "graph_id": "protected"}
    )
    assert calls == 1
    assert events[0]["status"] == "reduced"
    assert events[0]["after_size_bytes"] < events[0]["before_size_bytes"]


def test_shadow_reducer_failure_and_invalid_result_do_not_affect_original() -> None:
    class FailingTextReducer(TextReducer):
        async def reduce(self, result, budget_bytes):
            raise RuntimeError("shadow reducer details must not escape")

    class InvalidTextReducer(TextReducer):
        async def reduce(self, result, budget_bytes):
            result["ok"] = False
            return result

    original = tool_success({"summary": "text" * 100})
    paths = (("data", "summary"),)
    failed = asyncio.run(
        evaluate_output_budget_shadow_reduction(
            tool_name="get_semantic_graph",
            result=original,
            budget_bytes=10,
            reducers={"get_semantic_graph": FailingTextReducer(paths)},
        )
    )
    invalid = asyncio.run(
        evaluate_output_budget_shadow_reduction(
            tool_name="get_semantic_graph",
            result=original,
            budget_bytes=10,
            reducers={"get_semantic_graph": InvalidTextReducer(paths)},
        )
    )

    assert failed.status == "reducer_failed"
    assert invalid.status == "reducer_invalid"
    assert invalid.protected_fields_preserved is False
    assert original == tool_success({"summary": "text" * 100})


def test_shadow_registry_contains_only_allow_inventory_candidates() -> None:
    expected = {
        name
        for name, item in OUTPUT_REDUCER_INVENTORY.items()
        if item.review_status == "allow" and item.candidate_paths
    }
    assert set(OUTPUT_BUDGET_SHADOW_REDUCERS) == expected
    for name, reducer in OUTPUT_BUDGET_SHADOW_REDUCERS.items():
        assert isinstance(reducer, TextReducer)
        assert reducer.text_paths == OUTPUT_REDUCER_INVENTORY[name].candidate_paths


def test_default_enforcement_flag_off_keeps_original(monkeypatch) -> None:
    monkeypatch.delenv(OUTPUT_BUDGET_ENFORCEMENT_ENV, raising=False)

    async def handler(arguments, context):
        return {"ok": True, "summary": "original" * 100}

    tool = _tool("get_semantic_graph", handler)
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    _allow(monkeypatch)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    assert result["data"]["summary"] == "original" * 100


def test_shadow_is_deterministic_and_preserves_protected_fields() -> None:
    original = tool_success({"summary": "内容🙂" * 100, "execution_id": "keep"})

    observation = asyncio.run(
        evaluate_output_budget_shadow_reduction(
            tool_name="get_semantic_graph",
            result=original,
            budget_bytes=120,
        )
    )

    assert observation.status == "reduced"
    assert observation.deterministic is True
    assert observation.protected_fields_preserved is True


def test_non_deterministic_reducer_is_classified() -> None:
    class NonDeterministicTextReducer(TextReducer):
        calls = 0

        async def reduce(self, result, budget_bytes):
            type(self).calls += 1
            result["data"]["summary"] = f"candidate-{type(self).calls}"
            return result

    paths = (("data", "summary"),)
    observation = asyncio.run(
        evaluate_output_budget_shadow_reduction(
            tool_name="get_semantic_graph",
            result=tool_success({"summary": "original" * 100}),
            budget_bytes=120,
            reducers={"get_semantic_graph": NonDeterministicTextReducer(paths)},
        )
    )

    assert observation.status == "non_deterministic"
    assert observation.deterministic is False


def test_idempotency_replay_matches_original_result(
    idempotency_db, monkeypatch
) -> None:
    calls = 0

    async def handler(arguments, context):
        nonlocal calls
        calls += 1
        return {"ok": True, "summary": "cached-original" * 100}

    tool = _tool("get_semantic_graph", handler, idempotent=True)
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    _allow(monkeypatch)

    async def run_twice():
        first = await gateway.execute_tool(tool.name, {}, _context())
        replay = await gateway.execute_tool(tool.name, {}, _context())
        return first, replay

    first, replay = asyncio.run(run_twice())

    assert first == replay == tool_success({"summary": "cached-original" * 100})
    assert calls == 1


def test_shadow_evaluator_exception_isolated_from_gateway(monkeypatch) -> None:
    async def handler(arguments, context):
        return {"ok": True, "summary": "safe-original" * 100}

    async def explode(**values):
        raise RuntimeError("sensitive output")

    tool = _tool("get_semantic_graph", handler)
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    monkeypatch.setattr(gateway, "evaluate_output_budget_shadow_reduction", explode)
    _allow(monkeypatch)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    assert result["data"]["summary"] == "safe-original" * 100


def test_shadow_telemetry_does_not_log_output_arguments_or_identity(monkeypatch) -> None:
    messages = []

    class SafeLogger:
        def info(self, message, *args):
            messages.append((message, args))

        def warning(self, message, *args):
            messages.append((message, args))

    async def handler(arguments, context):
        return {"ok": True, "summary": "SECRET_OUTPUT" * 100}

    tool = _tool("get_semantic_graph", handler)
    tool.definition["function"]["parameters"] = {
        "type": "object",
        "properties": {"secret_argument": {"type": "string"}},
        "additionalProperties": False,
    }
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    monkeypatch.setattr(gateway, "logger", SafeLogger())
    _allow(monkeypatch)

    result = asyncio.run(
        gateway.execute_tool(tool.name, {"secret_argument": "SECRET_ARGUMENT"}, _context())
    )
    rendered = str(messages)

    assert result["data"]["summary"] == "SECRET_OUTPUT" * 100
    assert "SECRET_OUTPUT" not in rendered
    assert "SECRET_ARGUMENT" not in rendered
    assert "shadow-user" not in rendered
    assert "before_size_bytes" in rendered
