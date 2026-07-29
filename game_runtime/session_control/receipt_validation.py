"""Pure validation for untrusted Atomic Apply Receipt handoffs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from game_runtime.event import validate_control_result_event
from game_runtime.session_control.apply_contract import (
    CommittedResultEventReference,
    ControlApplyPlan,
    ControlRejectPlan,
)
from game_runtime.session_control.apply_plan_builder import BuildPlanReady, BuildReject
from game_runtime.session_control.coordinator_evidence import CoordinatorCommitReturned


class ReceiptInvalidReason(str, Enum):
    MISSING_IDENTITY_EVIDENCE = "MISSING_IDENTITY_EVIDENCE"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    COMMAND_MISMATCH = "COMMAND_MISMATCH"
    OPERATION_MISMATCH = "OPERATION_MISMATCH"
    CLAIM_MISMATCH = "CLAIM_MISMATCH"
    INPUT_EVENT_MISMATCH = "INPUT_EVENT_MISMATCH"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    CURSOR_MISMATCH = "CURSOR_MISMATCH"
    RESULT_EVENT_REFERENCE_MISMATCH = "RESULT_EVENT_REFERENCE_MISMATCH"
    TERMINAL_STATUS_MISMATCH = "TERMINAL_STATUS_MISMATCH"
    OWNERSHIP_MISMATCH = "OWNERSHIP_MISMATCH"


@dataclass(frozen=True, slots=True)
class ReceiptAccepted:
    """Validated runtime proof that an Atomic Apply commit is internally bound."""

    game_id: str
    session_id: str
    command_id: str
    operation_id: str
    claim_id: str
    input_event_id: str
    input_sequence_no: int
    committed_state_version: int
    committed_cursor: int
    ownership_generation: int | None
    commit_evidence_reference: str
    result_event_references: tuple[CommittedResultEventReference, ...]

    def __post_init__(self) -> None:
        for name in (
            "game_id",
            "session_id",
            "command_id",
            "operation_id",
            "claim_id",
            "input_event_id",
            "commit_evidence_reference",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty text")
        if (
            not isinstance(self.input_sequence_no, int)
            or isinstance(self.input_sequence_no, bool)
            or self.input_sequence_no <= 0
        ):
            raise ValueError("input_sequence_no must be a positive integer")
        for name in ("committed_state_version", "committed_cursor"):
            value = getattr(self, name)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer")
        if self.committed_cursor != self.input_sequence_no:
            raise ValueError("committed_cursor must equal input_sequence_no")
        if self.ownership_generation is not None and (
            not isinstance(self.ownership_generation, int)
            or isinstance(self.ownership_generation, bool)
            or self.ownership_generation <= 0
        ):
            raise ValueError("ownership_generation must be a positive integer")
        references = tuple(self.result_event_references)
        if not references or any(
            not isinstance(reference, CommittedResultEventReference)
            for reference in references
        ):
            raise ValueError(
                "result_event_references must contain committed references"
            )
        sequences = tuple(reference.sequence_no for reference in references)
        if (
            any(sequence <= self.input_sequence_no for sequence in sequences)
            or tuple(sorted(sequences)) != sequences
            or len(set(sequences)) != len(sequences)
        ):
            raise ValueError(
                "result Event sequences must be unique, ordered, and after input"
            )
        object.__setattr__(self, "result_event_references", references)


@dataclass(frozen=True, slots=True)
class ReceiptInvalid:
    reason: ReceiptInvalidReason

    def __post_init__(self) -> None:
        if not isinstance(self.reason, ReceiptInvalidReason):
            raise TypeError("reason must be a ReceiptInvalidReason")


ReceiptValidationResult = ReceiptAccepted | ReceiptInvalid


def validate_control_receipt(
    handoff: CoordinatorCommitReturned,
) -> ReceiptValidationResult:
    """Validate commit evidence without swapping State or causing notification."""

    if not isinstance(handoff, CoordinatorCommitReturned):
        raise TypeError("handoff must be a CoordinatorCommitReturned")
    claim = handoff.claim
    receipt = handoff.receipt
    outcome = handoff.build_outcome
    plan: ControlApplyPlan | ControlRejectPlan
    if isinstance(outcome, BuildPlanReady):
        plan = outcome.plan
    elif isinstance(outcome, BuildReject):
        plan = outcome.plan
    else:  # CoordinatorCommitReturned already closes this union.
        raise TypeError("handoff contains an unsupported build outcome")

    if any(
        value is None
        for value in (
            receipt.command_id,
            receipt.operation_id,
            receipt.operation_claim_id,
            receipt.input_event_id,
            receipt.input_sequence_no,
        )
    ) or not receipt.result_event_references:
        return ReceiptInvalid(ReceiptInvalidReason.MISSING_IDENTITY_EVIDENCE)

    if (
        claim.game_id,
        claim.session_id,
        receipt.game_id,
        receipt.session_id,
    ) != (plan.game_id, plan.session_id, plan.game_id, plan.session_id):
        return ReceiptInvalid(ReceiptInvalidReason.SCOPE_MISMATCH)
    if claim.command_id != plan.command_id or receipt.command_id != plan.command_id:
        return ReceiptInvalid(ReceiptInvalidReason.COMMAND_MISMATCH)
    if (
        claim.operation_id != plan.operation_id
        or receipt.operation_id != plan.operation_id
    ):
        return ReceiptInvalid(ReceiptInvalidReason.OPERATION_MISMATCH)
    if (
        claim.claim_id != plan.operation_claim_id
        or receipt.operation_claim_id != claim.claim_id
    ):
        return ReceiptInvalid(ReceiptInvalidReason.CLAIM_MISMATCH)
    if (
        claim.input_event_id != plan.input_event_id
        or receipt.input_event_id != plan.input_event_id
        or receipt.input_sequence_no != plan.input_sequence_no
    ):
        return ReceiptInvalid(ReceiptInvalidReason.INPUT_EVENT_MISMATCH)

    expected_version = (
        plan.candidate_snapshot.state_version
        if isinstance(plan, ControlApplyPlan)
        else plan.expected_state_version
    )
    if receipt.committed_state_version != expected_version:
        return ReceiptInvalid(ReceiptInvalidReason.VERSION_MISMATCH)
    if receipt.committed_cursor != plan.input_sequence_no:
        return ReceiptInvalid(ReceiptInvalidReason.CURSOR_MISMATCH)

    expected_events = (
        plan.result_events
        if isinstance(plan, ControlApplyPlan)
        else (plan.rejection_event,)
    )
    references = receipt.result_event_references
    if (
        len(references) != len(expected_events)
        or receipt.result_event_ids
        != tuple(event.event_id for event in expected_events)
        or any(
            reference.event_id != event.event_id
            or reference.event_type is not event.event_type
            or reference.stored_event_reference != event.event_id
            or reference.sequence_no <= plan.input_sequence_no
            or event.game_id != plan.game_id
            or event.session_id != plan.session_id
            or event.causation_event_id != plan.input_event_id
            or validate_control_result_event(event).command_id != plan.command_id
            or validate_control_result_event(event).operation_id != plan.operation_id
            or validate_control_result_event(event).input_event_id
            != plan.input_event_id
            for reference, event in zip(references, expected_events)
        )
        or tuple(reference.sequence_no for reference in references)
        != tuple(sorted(reference.sequence_no for reference in references))
        or len({reference.sequence_no for reference in references})
        != len(references)
    ):
        return ReceiptInvalid(
            ReceiptInvalidReason.RESULT_EVENT_REFERENCE_MISMATCH
        )
    if receipt.operation_status is not plan.operation_terminal_state:
        return ReceiptInvalid(ReceiptInvalidReason.TERMINAL_STATUS_MISMATCH)
    if (
        isinstance(plan, ControlApplyPlan)
        and receipt.ownership_generation
        != plan.ownership_intent.resulting_generation
    ):
        return ReceiptInvalid(ReceiptInvalidReason.OWNERSHIP_MISMATCH)

    return ReceiptAccepted(
        game_id=receipt.game_id,
        session_id=receipt.session_id,
        command_id=receipt.command_id,
        operation_id=receipt.operation_id,
        claim_id=receipt.operation_claim_id,
        input_event_id=receipt.input_event_id,
        input_sequence_no=receipt.input_sequence_no,
        committed_state_version=receipt.committed_state_version,
        committed_cursor=receipt.committed_cursor,
        ownership_generation=receipt.ownership_generation,
        commit_evidence_reference=receipt.commit_evidence_reference,
        result_event_references=references,
    )
