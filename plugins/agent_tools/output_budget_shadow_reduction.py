from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .contracts import ToolResult
from .output_budget import (
    OutputBudgetReducer,
    apply_output_budget_framework,
    serialized_tool_result_size_bytes,
)
from .output_reducer_inventory import OUTPUT_REDUCER_INVENTORY
from .reducers.output_budget_text import TextReducer


@dataclass(frozen=True)
class OutputBudgetShadowReductionResult:
    status: str
    reducer_type: str
    before_size_bytes: int
    after_size_bytes: int
    reduction_ratio: float
    protected_fields_preserved: bool
    deterministic: bool


# Shadow-only explicit allowlist. It is separate from OUTPUT_BUDGET_REDUCERS.
OUTPUT_BUDGET_SHADOW_REDUCERS: Mapping[str, OutputBudgetReducer] = MappingProxyType(
    {
        "generate_daily_report": TextReducer((("data", "report"),)),
        "build_semantic_graph": TextReducer((("data", "summary"),)),
        "get_semantic_graph": TextReducer((("data", "summary"),)),
        "render_semantic_graph": TextReducer((("data", "summary"),)),
    }
)


def _is_inventory_allowed(tool_name: str, reducer: OutputBudgetReducer) -> bool:
    item = OUTPUT_REDUCER_INVENTORY.get(tool_name)
    return bool(
        item is not None
        and item.review_status == "allow"
        and item.candidate_paths
        and isinstance(reducer, TextReducer)
        and reducer.text_paths == item.candidate_paths
    )


async def evaluate_output_budget_shadow_reduction(
    *,
    tool_name: str,
    result: ToolResult,
    budget_bytes: int,
    before_size_bytes: int | None = None,
    reducers: Mapping[str, OutputBudgetReducer] | None = None,
) -> OutputBudgetShadowReductionResult:
    registry = reducers if reducers is not None else OUTPUT_BUDGET_SHADOW_REDUCERS
    reducer = registry.get(tool_name)
    before = before_size_bytes
    if before is None:
        before = serialized_tool_result_size_bytes(result)
    if reducer is None or not _is_inventory_allowed(tool_name, reducer):
        return OutputBudgetShadowReductionResult(
            "not_allowed", "", before, before, 0.0, True, True
        )

    first = await apply_output_budget_framework(
        tool_name=tool_name,
        result=result,
        budget_bytes=budget_bytes,
        reducers={tool_name: reducer},
    )
    second = await apply_output_budget_framework(
        tool_name=tool_name,
        result=result,
        budget_bytes=budget_bytes,
        reducers={tool_name: reducer},
    )
    deterministic = first.status == second.status and first.result == second.result
    protected_fields_preserved = first.status != "reducer_invalid"
    status = first.status if deterministic else "non_deterministic"
    after = before
    if status == "reduced":
        after = serialized_tool_result_size_bytes(first.result)
    ratio = max(0.0, (before - after) / before) if before else 0.0
    return OutputBudgetShadowReductionResult(
        status,
        "text",
        before,
        after,
        ratio,
        protected_fields_preserved,
        deterministic,
    )
