from __future__ import annotations

import copy
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from .contracts import ToolResult, normalize_tool_result, tool_failure, tool_success


OUTPUT_BUDGET_ENFORCEMENT_ENV = "AGENT_OUTPUT_BUDGET_ENFORCEMENT"


class OutputBudgetReducer(Protocol):
    async def reduce(self, result: ToolResult, budget_bytes: int) -> ToolResult:
        """Return a deterministic, side-effect-free reduced ToolResult."""


@dataclass(frozen=True)
class OutputBudgetFrameworkResult:
    result: ToolResult
    status: str
    reducer_found: bool


OUTPUT_BUDGET_REDUCERS: Mapping[str, OutputBudgetReducer] = MappingProxyType({})


def output_budget_enforcement_enabled() -> bool:
    value = os.getenv(OUTPUT_BUDGET_ENFORCEMENT_ENV, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def serialized_tool_result_size_bytes(result: ToolResult) -> int:
    payload = json.dumps(
        result,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return len(payload.encode("utf-8"))


def canonicalize_output_budget_result(result: object) -> ToolResult | None:
    if not isinstance(result, dict):
        return None
    allowed_success_keys = {"ok", "data", "message"}
    allowed_failure_keys = {"ok", "error", "message", "retryable", "data"}
    if result.get("ok") is True:
        if not set(result).issubset(allowed_success_keys):
            return None
        data = result.get("data", {})
        if not isinstance(data, dict):
            return None
        return tool_success(copy.deepcopy(data), str(result.get("message") or ""))
    if result.get("ok") is False:
        if not set(result).issubset(allowed_failure_keys):
            return None
        data = result.get("data", {})
        if not isinstance(data, dict):
            return None
        normalized = normalize_tool_result(
            {
                "ok": False,
                "error": result.get("error"),
                "message": result.get("message"),
                "retryable": result.get("retryable") is True,
            }
        )
        if normalized.get("error") != result.get("error"):
            return None
        if data:
            normalized["data"] = copy.deepcopy(data)
        return normalized
    return None


def _protected_values(result: ToolResult) -> dict[tuple[str, ...], object]:
    protected: dict[tuple[str, ...], object] = {}

    def visit(value: object, path: tuple[str, ...]) -> None:
        if not isinstance(value, dict):
            return
        for raw_key, item in value.items():
            key = str(raw_key)
            item_path = (*path, key)
            lowered = key.lower()
            if (
                key in {"ok", "error", "retryable"}
                or "execution" in lowered
                or "idempotency" in lowered
                or "confirmation" in lowered
                or lowered in {"invocation_id", "tool_call_id"}
            ):
                protected[item_path] = item
            visit(item, item_path)

    visit(result, ())
    return protected


def _preserves_protected_values(original: ToolResult, reduced: ToolResult) -> bool:
    original_values = _protected_values(original)
    reduced_values = _protected_values(reduced)
    return original_values == reduced_values


async def apply_output_budget_framework(
    *,
    tool_name: str,
    result: ToolResult,
    budget_bytes: int,
    reducers: Mapping[str, OutputBudgetReducer] | None = None,
) -> OutputBudgetFrameworkResult:
    registry = reducers if reducers is not None else OUTPUT_BUDGET_REDUCERS
    reducer = registry.get(tool_name)
    if reducer is None:
        return OutputBudgetFrameworkResult(result, "reducer_missing", False)

    try:
        candidate = await reducer.reduce(copy.deepcopy(result), budget_bytes)
    except Exception:
        return OutputBudgetFrameworkResult(result, "reducer_failed", True)

    try:
        reduced = canonicalize_output_budget_result(candidate)
        preserves_values = reduced is not None and _preserves_protected_values(result, reduced)
    except Exception:
        return OutputBudgetFrameworkResult(result, "reducer_invalid", True)
    if reduced is None or not preserves_values:
        return OutputBudgetFrameworkResult(result, "reducer_invalid", True)
    return OutputBudgetFrameworkResult(reduced, "reduced", True)
