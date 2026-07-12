from __future__ import annotations

import asyncio
import copy

import pytest

from plugins.agent_tools.contracts import tool_success
from plugins.agent_tools.output_budget import (
    OUTPUT_BUDGET_REDUCERS,
    apply_output_budget_framework,
    serialized_tool_result_size_bytes,
)
from plugins.agent_tools.output_reducer_capability import OUTPUT_REDUCER_CAPABILITIES
from plugins.agent_tools.output_reducer_capability_review import (
    OUTPUT_REDUCER_CAPABILITY_PROPOSALS,
)
from plugins.agent_tools.output_reducer_inventory import OUTPUT_REDUCER_INVENTORY
from plugins.agent_tools.reducers.output_budget_text import TextReducer


def _result(text: object = "summary" * 500, *, fixed_size: int = 20):
    return tool_success(
        {
            "graph_id": "graph-review",
            "group_id": "group-review",
            "nodes": [{"label": "node", "description": "N" * fixed_size}],
            "edges": [{"source": "node", "target": "other", "weight": 1}],
            "summary": text,
        }
    )


def test_proposals_match_allow_inventory_without_registering_capabilities() -> None:
    expected = {
        name
        for name, item in OUTPUT_REDUCER_INVENTORY.items()
        if item.review_status == "allow" and item.candidate_paths
    }
    assert set(OUTPUT_REDUCER_CAPABILITY_PROPOSALS) == expected
    for name, proposal in OUTPUT_REDUCER_CAPABILITY_PROPOSALS.items():
        inventory = OUTPUT_REDUCER_INVENTORY[name]
        assert proposal.allowed_paths == inventory.candidate_paths
        assert proposal.reducer_type == "text"
        assert proposal.review_status == "schema_aligned_pending_registration_review"
        assert proposal.registration_blockers
        assert proposal.budget_rationale
        assert proposal.fallback_policy
        assert proposal.handler_boundary
    assert dict(OUTPUT_REDUCER_CAPABILITIES) == {}
    assert dict(OUTPUT_BUDGET_REDUCERS) == {}


@pytest.mark.parametrize("tool_name", ["get_group_profile", "get_member_profile"])
def test_review_inventory_tools_cannot_generate_proposal(tool_name: str) -> None:
    assert OUTPUT_REDUCER_INVENTORY[tool_name].review_status == "review"
    assert tool_name not in OUTPUT_REDUCER_CAPABILITY_PROPOSALS


def test_budget_case_a_over_budget_allows_reduction() -> None:
    original = _result()
    reducer = TextReducer((("data", "summary"),))
    fixed = _result("")
    budget = serialized_tool_result_size_bytes(fixed) + 80

    outcome = asyncio.run(
        apply_output_budget_framework(
            tool_name="get_semantic_graph",
            result=original,
            budget_bytes=budget,
            reducers={"get_semantic_graph": reducer},
        )
    )
    assert outcome.status == "reduced"
    assert outcome.result != original
    assert serialized_tool_result_size_bytes(outcome.result) <= budget


def test_budget_case_b_fixed_structure_over_budget_falls_back_original() -> None:
    original = _result(fixed_size=5000)
    snapshot = copy.deepcopy(original)
    reducer = TextReducer((("data", "summary"),))
    fixed_size = serialized_tool_result_size_bytes(_result("", fixed_size=5000))

    reduced = asyncio.run(reducer.reduce(original, fixed_size - 1))
    assert reduced == original == snapshot


def test_budget_case_c_still_over_budget_falls_back_original() -> None:
    original = _result(text=["summary is no longer a string"] * 100, fixed_size=1000)
    snapshot = copy.deepcopy(original)
    reducer = TextReducer((("data", "summary"),))
    budget = serialized_tool_result_size_bytes(original) - 1

    reduced = asyncio.run(reducer.reduce(original, budget))
    assert serialized_tool_result_size_bytes(reduced) > budget
    assert reduced == original == snapshot


def test_proposed_text_reducer_preserves_tool_contract() -> None:
    original = _result()
    budget = serialized_tool_result_size_bytes(_result("")) + 100
    reduced = asyncio.run(TextReducer((("data", "summary"),)).reduce(original, budget))

    assert reduced["ok"] is True
    assert isinstance(reduced["data"], dict)
    assert isinstance(reduced["data"]["summary"], str)
    assert reduced["data"]["summary"].endswith("…")
    for field in ("graph_id", "group_id", "nodes", "edges"):
        assert reduced["data"][field] == original["data"][field]
