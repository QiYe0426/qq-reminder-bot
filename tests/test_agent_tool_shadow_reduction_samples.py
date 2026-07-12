from __future__ import annotations

import asyncio
import copy
from collections.abc import Mapping

import pytest

from plugins.agent_tools.contracts import ToolResult, tool_success
from plugins.agent_tools.output_budget import (
    OUTPUT_BUDGET_REDUCERS,
    apply_output_budget_framework,
    serialized_tool_result_size_bytes,
)
from plugins.agent_tools.output_budget_shadow_reduction import (
    OUTPUT_BUDGET_SHADOW_REDUCERS,
    evaluate_output_budget_shadow_reduction,
)
from plugins.agent_tools.output_reducer_capability import OUTPUT_REDUCER_CAPABILITIES
from plugins.agent_tools.reducers.output_budget_text import TextReducer


SAMPLE_BUDGET_BYTES = 900
PROTECTED_FIELD_NAMES = (
    "ok",
    "error",
    "retryable",
    "execution",
    "confirmation",
    "idempotency",
)


def _long_sections(prefix: str) -> list[dict[str, object]]:
    return [
        {
            "heading": f"{prefix} section {index}",
            "description": (f"{prefix} 的长 description {index}，包含中文与 emoji 🙂。" * 18),
            "paragraphs": [
                f"第 {paragraph} 段：{prefix} 多段文本列表内容。" * 12
                for paragraph in range(3)
            ],
            "metrics": {"rank": index, "weights": [1, 2, 3]},
        }
        for index in range(3)
    ]


def _sample(tool_name: str) -> ToolResult:
    common = {
        "group_id": "canonical-group",
        "metadata": {
            "description": "固定的长 description；它位于 candidate path 外。" * 20,
            "sections": _long_sections(tool_name),
        },
    }
    if tool_name == "generate_daily_report":
        return tool_success(
            {
                **common,
                "date": "2026-07-12",
                "filename": "daily-report.txt",
                "image_filename": "daily-report.png",
                "pdf_filename": "daily-report.pdf",
                "report": "日报长文本 summary：" * 500,
                "truncated": False,
                "preview_chars": 1200,
                "reused_existing": False,
            }
        )
    if tool_name in {"build_semantic_graph", "get_semantic_graph"}:
        return tool_success(
            {
                **common,
                "graph_id": "canonical-graph",
                "nodes": [
                    {"id": "node-a", "label": "主题 A", "attributes": {"score": 0.9}},
                    {"id": "node-b", "label": "主题 B", "attributes": {"score": 0.8}},
                ],
                "edges": [{"source": "node-a", "target": "node-b", "weight": 3}],
                "summary": "语义图长文本 summary：" * 500,
            }
        )
    if tool_name == "render_semantic_graph":
        return tool_success(
            {
                **common,
                "graph_id": "canonical-graph",
                "image_filename": "semantic-graph.png",
                "image_path": "artifacts/semantic-graph.png",
                "sent": False,
                "send_error": "",
                "summary": "渲染结果长文本 summary：" * 500,
            }
        )
    raise AssertionError(f"Missing canonical sample for {tool_name}")


def _without_path(value: ToolResult, path: tuple[str, ...]) -> ToolResult:
    copied = copy.deepcopy(value)
    parent: object = copied
    for part in path[:-1]:
        assert isinstance(parent, dict)
        parent = parent[part]
    assert isinstance(parent, dict)
    parent.pop(path[-1])
    return copied


def _reducible_budget(value: ToolResult, path: tuple[str, ...]) -> int:
    fixed = copy.deepcopy(value)
    parent: object = fixed
    for part in path[:-1]:
        assert isinstance(parent, dict)
        parent = parent[part]
    assert isinstance(parent, dict)
    parent[path[-1]] = ""
    return serialized_tool_result_size_bytes(fixed) + 600


@pytest.mark.parametrize("tool_name", tuple(OUTPUT_BUDGET_SHADOW_REDUCERS))
def test_allow_candidate_canonical_samples_pass_shadow_validation(tool_name: str) -> None:
    original = _sample(tool_name)
    original_snapshot = copy.deepcopy(original)
    reducer = OUTPUT_BUDGET_SHADOW_REDUCERS[tool_name]
    candidate_path = reducer.text_paths[0]
    budget_bytes = _reducible_budget(original, candidate_path)

    first = asyncio.run(reducer.reduce(original, budget_bytes))
    second = asyncio.run(reducer.reduce(original, budget_bytes))
    observation = asyncio.run(
        evaluate_output_budget_shadow_reduction(
            tool_name=tool_name,
            result=original,
            budget_bytes=budget_bytes,
        )
    )

    assert first == second
    assert serialized_tool_result_size_bytes(original) > serialized_tool_result_size_bytes(first)
    assert observation.status == "reduced"
    assert observation.deterministic is True
    assert observation.protected_fields_preserved is True
    assert observation.after_size_bytes < observation.before_size_bytes
    assert _without_path(first, candidate_path) == _without_path(original, candidate_path)
    for field in PROTECTED_FIELD_NAMES:
        assert first.get(field) == original.get(field)
    assert original == original_snapshot


def test_review_candidate_does_not_enter_shadow() -> None:
    original = tool_success(
        {"group_id": "canonical-group", "profile": {"summary": "review" * 500}}
    )
    observation = asyncio.run(
        evaluate_output_budget_shadow_reduction(
            tool_name="get_group_profile",
            result=original,
            budget_bytes=SAMPLE_BUDGET_BYTES,
            reducers={
                "get_group_profile": TextReducer((("data", "profile", "summary"),))
            },
        )
    )
    assert observation.status == "not_allowed"
    assert observation.before_size_bytes == observation.after_size_bytes


def test_disabled_reducer_does_not_execute() -> None:
    class CountingReducer:
        calls = 0

        async def reduce(self, result, budget_bytes):
            type(self).calls += 1
            return result

    original = _sample("get_semantic_graph")
    observation = asyncio.run(
        evaluate_output_budget_shadow_reduction(
            tool_name="get_semantic_graph",
            result=original,
            budget_bytes=SAMPLE_BUDGET_BYTES,
            reducers={"get_semantic_graph": CountingReducer()},
        )
    )
    assert observation.status == "not_allowed"
    assert CountingReducer.calls == 0
    assert dict(OUTPUT_BUDGET_REDUCERS) == {}
    assert dict(OUTPUT_REDUCER_CAPABILITIES) == {}


class _FailingTextReducer(TextReducer):
    async def reduce(self, result, budget_bytes):
        raise RuntimeError("sample shadow failure")


class _NonDeterministicTextReducer(TextReducer):
    calls = 0

    async def reduce(self, result, budget_bytes):
        type(self).calls += 1
        candidate = copy.deepcopy(result)
        candidate["data"]["summary"] = f"candidate-{type(self).calls}"
        return candidate


class _ProtectedFieldViolatingTextReducer(TextReducer):
    async def reduce(self, result, budget_bytes):
        candidate = copy.deepcopy(result)
        candidate["ok"] = False
        return candidate


@pytest.mark.parametrize(
    ("reducer", "expected_status"),
    [
        (_FailingTextReducer((("data", "summary"),)), "reducer_failed"),
        (_NonDeterministicTextReducer((("data", "summary"),)), "non_deterministic"),
        (_ProtectedFieldViolatingTextReducer((("data", "summary"),)), "reducer_invalid"),
    ],
)
def test_shadow_failure_modes_fallback_to_original(
    reducer: TextReducer, expected_status: str
) -> None:
    original = _sample("get_semantic_graph")
    snapshot = copy.deepcopy(original)
    observation = asyncio.run(
        evaluate_output_budget_shadow_reduction(
            tool_name="get_semantic_graph",
            result=original,
            budget_bytes=SAMPLE_BUDGET_BYTES,
            reducers={"get_semantic_graph": reducer},
        )
    )
    assert observation.status == expected_status
    assert original == snapshot


@pytest.mark.parametrize("tool_name", tuple(OUTPUT_BUDGET_SHADOW_REDUCERS))
def test_shadow_candidate_never_replaces_cache_or_replay_value(tool_name: str) -> None:
    original = _sample(tool_name)
    reducer = OUTPUT_BUDGET_SHADOW_REDUCERS[tool_name]
    budget_bytes = _reducible_budget(original, reducer.text_paths[0])
    cache: dict[str, ToolResult] = {}

    async def execute_and_cache() -> tuple[ToolResult, ToolResult]:
        normalized = copy.deepcopy(original)
        await evaluate_output_budget_shadow_reduction(
            tool_name=tool_name,
            result=normalized,
            budget_bytes=budget_bytes,
        )
        cache[tool_name] = copy.deepcopy(normalized)
        return normalized, copy.deepcopy(cache[tool_name])

    first, replay = asyncio.run(execute_and_cache())
    assert first == original
    assert cache[tool_name] == original
    assert replay == original


def test_production_output_reduction_remains_disabled() -> None:
    original = _sample("generate_daily_report")
    outcome = asyncio.run(
        apply_output_budget_framework(
            tool_name="generate_daily_report",
            result=original,
            budget_bytes=SAMPLE_BUDGET_BYTES,
        )
    )
    assert outcome.status == "reducer_missing"
    assert outcome.result == original
    assert dict(OUTPUT_BUDGET_REDUCERS) == {}
    assert dict(OUTPUT_REDUCER_CAPABILITIES) == {}
