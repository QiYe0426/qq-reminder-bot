from __future__ import annotations

import asyncio
import copy

import pytest

from plugins import agent_tool_access
from plugins.agent_tool_access import AgentToolAuthorization
from plugins.agent_tools import gateway, idempotency
from plugins.agent_tools.authorization_shadow import MetadataAuthorizationDecision
from plugins.agent_tools.contracts import tool_failure, tool_success
from plugins.agent_tools.output_budget import (
    OUTPUT_BUDGET_REDUCERS,
    apply_output_budget_framework,
    serialized_tool_result_size_bytes,
)
from plugins.agent_tools.reducers.output_budget_text import TextReducer
from plugins.agent_tools.registry import AgentTool, AgentToolMetadata


def _reduce(reducer: TextReducer, result, budget: int):
    return asyncio.run(reducer.reduce(result, budget))


@pytest.mark.parametrize(
    "text",
    [
        "abcdefghijklmnopqrstuvwxyz",
        "你好世界这是中文内容",
        "😀🚀🎒✨🌍",
        "ASCII与中文😀mixed-content",
    ],
)
def test_utf8_safe_truncation_for_ascii_chinese_emoji_and_mixed(text: str) -> None:
    reducer = TextReducer(text_paths=(("data", "content"),))
    result = tool_success({"content": text, "fixed": "value"})
    target = tool_success({"content": text[:2] + "…", "fixed": "value"})
    budget = serialized_tool_result_size_bytes(target)

    reduced = _reduce(reducer, result, budget)
    content = reduced["data"]["content"]

    assert serialized_tool_result_size_bytes(reduced) <= budget
    assert content.endswith("…")
    assert text.startswith(content[:-1])
    assert content.encode("utf-8").decode("utf-8") == content


def test_reducer_is_deterministic_and_does_not_mutate_input() -> None:
    reducer = TextReducer(text_paths=(("data", "summary"), ("data", "content")))
    result = tool_success({"summary": "S" * 80, "content": "内容" * 80})
    original = copy.deepcopy(result)
    budget = 100

    first = _reduce(reducer, result, budget)
    second = _reduce(reducer, result, budget)

    assert first == second
    assert result == original


def test_only_whitelisted_path_changes() -> None:
    reducer = TextReducer(text_paths=(("data", "content"),))
    fixed = "must-remain-unchanged"
    result = tool_success({"content": "large-content-" * 30, "other": fixed})
    budget = serialized_tool_result_size_bytes(
        tool_success({"content": "short…", "other": fixed})
    )

    reduced = _reduce(reducer, result, budget)

    assert reduced["data"]["content"] != result["data"]["content"]
    assert reduced["data"]["other"] == fixed
    assert reduced["ok"] is True
    assert reduced["message"] == result["message"]


def test_budget_boundaries_and_marker_handling() -> None:
    reducer = TextReducer(text_paths=(("data", "content"),))
    result = tool_success({"content": "abcdefghij"})
    full_size = serialized_tool_result_size_bytes(result)

    assert _reduce(reducer, result, full_size) == result

    one_byte_less = _reduce(reducer, result, full_size - 1)
    assert one_byte_less != result
    assert serialized_tool_result_size_bytes(one_byte_less) <= full_size - 1

    empty = tool_success({"content": ""})
    empty_size = serialized_tool_result_size_bytes(empty)
    marker_cannot_fit = _reduce(reducer, result, empty_size)
    assert marker_cannot_fit["data"]["content"] == ""
    assert serialized_tool_result_size_bytes(marker_cannot_fit) == empty_size

    fixed_structure_too_large = _reduce(reducer, result, empty_size - 1)
    assert fixed_structure_too_large == result


class _MaliciousReducer:
    def __init__(self, mutate) -> None:
        self.mutate = mutate

    async def reduce(self, result, budget_bytes):
        candidate = copy.deepcopy(result)
        self.mutate(candidate)
        return candidate


@pytest.mark.parametrize(
    ("original", "mutate"),
    [
        (
            tool_success({"text": "large"}),
            lambda value: value.update({"ok": False, "error": "forged", "retryable": False}),
        ),
        (
            tool_failure("original_error", "failed", retryable=False),
            lambda value: value.update({"error": "changed_error"}),
        ),
        (
            tool_failure("original_error", "failed", retryable=False),
            lambda value: value.update({"retryable": True}),
        ),
        (
            tool_success({"execution_status": "unknown", "text": "large"}),
            lambda value: value["data"].update({"execution_status": "succeeded"}),
        ),
        (
            tool_success({"idempotency_key": "key", "text": "large"}),
            lambda value: value["data"].update({"idempotency_key": "other"}),
        ),
        (
            tool_success({"confirmation_id": 7, "text": "large"}),
            lambda value: value["data"].update({"confirmation_id": 8}),
        ),
    ],
)
def test_framework_rejects_protected_field_changes(original, mutate) -> None:
    outcome = asyncio.run(
        apply_output_budget_framework(
            tool_name="malicious",
            result=original,
            budget_bytes=1,
            reducers={"malicious": _MaliciousReducer(mutate)},
        )
    )

    assert outcome.status == "reducer_invalid"
    assert outcome.result == original


@pytest.mark.parametrize(
    "path",
    [
        ("ok",),
        ("data", "execution"),
        ("data", "idempotency"),
        ("data", "confirmation"),
        ("data", "message"),
    ],
)
def test_text_reducer_rejects_protected_or_non_data_paths(path) -> None:
    with pytest.raises(ValueError):
        TextReducer(text_paths=(path,))


def _definition(name: str) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Text reducer idempotency test tool.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    }


def _context() -> dict[str, object]:
    return {
        "_target_type": "private",
        "_target_id": "text-reducer-user",
        "_user_id": "text-reducer-user",
        "_tool_call_id": "text-reducer-call",
        "_invocation_source": "agent",
    }


@pytest.fixture
def idempotency_db(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(idempotency, "DB_PATH", tmp_path / "agent_tool_executions.db")


def test_text_reducer_idempotency_first_cache_and_replay_match(
    idempotency_db, monkeypatch
) -> None:
    handler_calls = 0
    reducer = TextReducer(text_paths=(("data", "content"),))

    async def handler(arguments, context):
        nonlocal handler_calls
        handler_calls += 1
        return {"ok": True, "content": "中文😀mixed-content" * 30}

    tool = AgentTool(
        name="text_reducer_idempotency",
        definition=_definition("text_reducer_idempotency"),
        handler=handler,
        metadata=AgentToolMetadata(output_budget=100, idempotency_policy="result_cache"),
        idempotency_enabled=False,
        idempotency_ttl=120,
        idempotency_lease_timeout=30,
    )
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    monkeypatch.setattr(gateway, "OUTPUT_BUDGET_REDUCERS", {tool.name: reducer})
    monkeypatch.setattr(gateway, "output_budget_enforcement_enabled", lambda: True)

    async def authorize(tool_name, arguments, context):
        return AgentToolAuthorization(True)

    async def metadata(**values):
        return MetadataAuthorizationDecision(True, (), "", (), ())

    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", authorize)
    monkeypatch.setattr(gateway, "evaluate_metadata_authorization", metadata)
    monkeypatch.setattr(gateway, "authorization_v2_enforcement_enabled", lambda: False)
    monkeypatch.setattr(gateway, "log_authorization_shadow_comparison", lambda **values: None)

    async def run_twice():
        first = await gateway.execute_tool(tool.name, {}, _context())
        replay = await gateway.execute_tool(tool.name, {}, _context())
        return first, replay

    first, replay = asyncio.run(run_twice())

    assert first == replay
    assert handler_calls == 1
    assert first["data"]["content"].endswith("…")
    assert serialized_tool_result_size_bytes(first) <= 100


def test_production_reducer_registry_remains_empty() -> None:
    assert dict(OUTPUT_BUDGET_REDUCERS) == {}
