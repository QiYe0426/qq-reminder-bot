from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .output_reducer_capability import OutputPath


@dataclass(frozen=True)
class OutputReducerCapabilityProposal:
    """Non-enforcing review record for a possible future capability."""

    tool_name: str
    allowed_paths: tuple[OutputPath, ...]
    reducer_type: str
    max_chars: int | None
    proposed_budget_bytes: int
    review_status: str
    registration_blockers: tuple[str, ...]
    budget_rationale: str
    fallback_policy: str
    handler_boundary: str


# Audit proposals only. They are deliberately separate from
# OUTPUT_REDUCER_CAPABILITIES and OUTPUT_BUDGET_REDUCERS.
_PROPOSALS = (
    OutputReducerCapabilityProposal(
        tool_name="generate_daily_report",
        allowed_paths=(("data", "report"),),
        reducer_type="text",
        max_chars=6000,
        proposed_budget_bytes=5000,
        review_status="schema_aligned_pending_registration_review",
        registration_blockers=(
            "declare_and_review_output_budget",
            "production_registration_review",
        ),
        budget_rationale="5000 bytes limits model context while retaining the canonical fixed report metadata; production sizing still requires observed handler-shaped distributions.",
        fallback_policy="Return original when fixed fields plus an empty report exceed the budget.",
        handler_boundary="The handler's 6000-character limit controls report preview semantics; a future byte reducer would only enforce canonical serialized size afterward.",
    ),
    OutputReducerCapabilityProposal(
        tool_name="build_semantic_graph",
        allowed_paths=(("data", "summary"),),
        reducer_type="text",
        max_chars=None,
        proposed_budget_bytes=5000,
        review_status="schema_aligned_pending_registration_review",
        registration_blockers=(
            "declare_and_review_output_budget",
            "production_registration_review",
        ),
        budget_rationale="5000 bytes matches the existing read/render graph proposal, but must be validated against real topology sizes before metadata approval.",
        fallback_policy="Return original when protected graph identity, topology, counts and time already exceed the budget.",
        handler_boundary="The adapter generates summary text; a future reducer may shorten only that summary and never graph topology.",
    ),
    OutputReducerCapabilityProposal(
        tool_name="get_semantic_graph",
        allowed_paths=(("data", "summary"),),
        reducer_type="text",
        max_chars=None,
        proposed_budget_bytes=5000,
        review_status="schema_aligned_pending_registration_review",
        registration_blockers=("production_registration_review",),
        budget_rationale="5000 bytes is already declared in metadata and bounds the model-facing persisted graph result.",
        fallback_policy="Return original when protected persisted graph fields already exceed 5000 bytes.",
        handler_boundary="The adapter regenerates summary from loaded graph data; reduction remains a later byte-budget concern.",
    ),
    OutputReducerCapabilityProposal(
        tool_name="render_semantic_graph",
        allowed_paths=(("data", "summary"),),
        reducer_type="text",
        max_chars=None,
        proposed_budget_bytes=5000,
        review_status="schema_aligned_pending_registration_review",
        registration_blockers=("production_registration_review",),
        budget_rationale="5000 bytes is already declared and normally leaves room for the fixed render and delivery fields.",
        fallback_policy="Return original when protected file, identity and delivery fields already exceed 5000 bytes.",
        handler_boundary="The adapter selects a source summary or regenerates one; a future reducer may only shorten the selected summary afterward.",
    ),
)


OUTPUT_REDUCER_CAPABILITY_PROPOSALS: Mapping[
    str, OutputReducerCapabilityProposal
] = MappingProxyType({proposal.tool_name: proposal for proposal in _PROPOSALS})
