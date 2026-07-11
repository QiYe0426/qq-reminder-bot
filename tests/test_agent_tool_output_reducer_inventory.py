from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from plugins.agent_tools import list_agent_tools
from plugins.agent_tools.contracts import tool_success
from plugins.agent_tools.output_budget import OUTPUT_BUDGET_REDUCERS, apply_output_budget_framework
from plugins.agent_tools.output_reducer_capability import OUTPUT_REDUCER_CAPABILITIES, OutputReducerCapability
from plugins.agent_tools.output_reducer_inventory import OUTPUT_REDUCER_INVENTORY, OutputReducerInventoryItem, list_output_reducer_inventory


def _item(**updates: object) -> OutputReducerInventoryItem:
    values = dict(tool_name="example", output_structure=(("data", "content"), ("data", "resource_id")), candidate_paths=(("data", "content"),), blocked_paths=(("data", "resource_id"),), risk_level="medium", reason="Explicit test audit.", review_status="allow")
    values.update(updates)
    return OutputReducerInventoryItem(**values)


def test_inventory_covers_registered_tools_and_is_deterministic() -> None:
    registered = {tool.name for tool in list_agent_tools()}
    assert set(OUTPUT_REDUCER_INVENTORY) == registered
    assert tuple(item.tool_name for item in list_output_reducer_inventory()) == tuple(sorted(registered))


def test_inventory_is_immutable() -> None:
    item = _item()
    with pytest.raises(FrozenInstanceError):
        item.review_status = "deny"  # type: ignore[misc]
    with pytest.raises(TypeError):
        OUTPUT_REDUCER_INVENTORY["example"] = item  # type: ignore[index]


@pytest.mark.parametrize("path", [("error",), ("execution", "id"), ("idempotency", "key"), ("confirmation", "id")])
def test_candidate_paths_must_use_data_namespace(path: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="data namespace"):
        _item(output_structure=(path,), candidate_paths=(path,), blocked_paths=())


def test_blocked_path_cannot_also_be_candidate() -> None:
    with pytest.raises(ValueError, match="must not overlap"):
        _item(blocked_paths=(("data", "content"),))


def test_inventory_and_capability_models_are_consistent() -> None:
    assert dict(OUTPUT_REDUCER_CAPABILITIES) == {}
    for item in OUTPUT_REDUCER_INVENTORY.values():
        for path in item.candidate_paths:
            capability = OutputReducerCapability(item.tool_name, "text", (path,), enabled=False, risk_level=item.risk_level)
            assert capability.allowed_paths == (path,)
        assert not (set(item.candidate_paths) & set(item.blocked_paths))


def test_inventory_does_not_register_or_trigger_reducer() -> None:
    assert dict(OUTPUT_BUDGET_REDUCERS) == {}
    original = tool_success({"content": "unchanged"})
    result = asyncio.run(apply_output_budget_framework(tool_name="generate_daily_report", result=original, budget_bytes=1))
    assert result.status == "reducer_missing"
    assert result.result == original
