"""Actor-owned completion boundary for one composite Game Runtime state cell."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from game_runtime.event import (
    GameEvent,
    GameEventType,
    validate_control_result_event,
)
from game_runtime.session_control.actor_visible_game_state import (
    ActorVisibleGameState,
    ControlCompletionIdentity,
    ControlCompletionKind,
)
from game_runtime.session_control.apply_contract import (
    ControlApplyPlan,
    ControlRejectPlan,
    OwnershipIntentType,
)
from game_runtime.session_control.apply_plan_builder import BuildPlanReady, BuildReject
from game_runtime.session_control.composite_snapshot import CandidateGameSnapshot
from game_runtime.session_control.control_reject_completion import (
    CommittedControlRejectAccepted,
    ControlTurnAcceptance,
)
from game_runtime.session_control.control_turn_contract import (
    ActorControlCommitBoundaryError,
    ControlTurnCommitReady,
)
from game_runtime.session_control.lifecycle_snapshot_boundary import (
    LifecycleSnapshotIdentity,
    SnapshotVisibilityAccepted,
)
from game_runtime.session_control.receipt_validation import ReceiptAccepted


class ActorGameStateCompletionFailureReason(str, Enum):
    INVALID_COMMIT_READY = "INVALID_COMMIT_READY"
    UNSUPPORTED_BUILD_OUTCOME = "UNSUPPORTED_BUILD_OUTCOME"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    OPERATION_IDENTITY_MISMATCH = "OPERATION_IDENTITY_MISMATCH"
    RECEIPT_BINDING_MISMATCH = "RECEIPT_BINDING_MISMATCH"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    CONTROL_CURSOR_MISMATCH = "CONTROL_CURSOR_MISMATCH"
    MATERIALIZATION_CURSOR_MISMATCH = "MATERIALIZATION_CURSOR_MISMATCH"
    OWNERSHIP_MISMATCH = "OWNERSHIP_MISMATCH"
    SNAPSHOT_IDENTITY_MISMATCH = "SNAPSHOT_IDENTITY_MISMATCH"
    COMPLETION_IDENTITY_CONFLICT = "COMPLETION_IDENTITY_CONFLICT"
    STATE_REPLACEMENT_FAILED = "STATE_REPLACEMENT_FAILED"


class ActorGameStateCompletionBoundaryError(ActorControlCommitBoundaryError):
    """A committed Control Turn cannot safely enter Actor visibility."""

    def __init__(self, reason: ActorGameStateCompletionFailureReason) -> None:
        if not isinstance(reason, ActorGameStateCompletionFailureReason):
            raise TypeError(
                "reason must be an ActorGameStateCompletionFailureReason"
            )
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True)
class _ActorGameStateCell:
    state: ActorVisibleGameState
    last_acceptance: ControlTurnAcceptance | None


class ActorOwnedGameStateCompletionBoundary:
    """Publish committed Apply or Reject outcomes into one immutable state cell."""

    __slots__ = ("_cell",)

    def __init__(self, *, initial_state: ActorVisibleGameState) -> None:
        if not isinstance(initial_state, ActorVisibleGameState):
            raise TypeError("initial_state must be an ActorVisibleGameState")
        self._cell = _ActorGameStateCell(
            state=initial_state,
            last_acceptance=None,
        )

    @property
    def current_state(self) -> ActorVisibleGameState:
        return self._cell.state

    def accept(self, commit_ready: ControlTurnCommitReady) -> ControlTurnAcceptance:
        if not isinstance(commit_ready, ControlTurnCommitReady):
            _fail(ActorGameStateCompletionFailureReason.INVALID_COMMIT_READY)
        outcome = commit_ready.build_outcome
        if not isinstance(outcome, (BuildPlanReady, BuildReject)):
            _fail(ActorGameStateCompletionFailureReason.UNSUPPORTED_BUILD_OUTCOME)
        if not isinstance(commit_ready.accepted_receipt, ReceiptAccepted):
            _fail(
                ActorGameStateCompletionFailureReason.RECEIPT_BINDING_MISMATCH
            )

        completion = _completion_identity(commit_ready)
        duplicate = self._duplicate_acceptance(completion)
        if duplicate is not None:
            return duplicate
        if isinstance(outcome, BuildPlanReady):
            next_state, acceptance = self._accept_applied(commit_ready, completion)
        else:
            next_state, acceptance = self._accept_rejected(commit_ready, completion)
        replacement = _ActorGameStateCell(
            state=next_state,
            last_acceptance=acceptance,
        )
        try:
            self._cell = replacement
        except Exception as exc:
            raise ActorGameStateCompletionBoundaryError(
                ActorGameStateCompletionFailureReason.STATE_REPLACEMENT_FAILED
            ) from exc
        if self._cell is not replacement:
            _fail(ActorGameStateCompletionFailureReason.STATE_REPLACEMENT_FAILED)
        return acceptance

    def _duplicate_acceptance(
        self,
        completion: ControlCompletionIdentity,
    ) -> ControlTurnAcceptance | None:
        previous = self._cell.state.last_completion_identity
        if previous is None or previous.operation_id != completion.operation_id:
            return None
        if previous != completion or self._cell.last_acceptance is None:
            _fail(
                ActorGameStateCompletionFailureReason.COMPLETION_IDENTITY_CONFLICT
            )
        accepted = self._cell.last_acceptance
        if isinstance(accepted, SnapshotVisibilityAccepted):
            return replace(accepted, already_visible=True)
        if isinstance(accepted, CommittedControlRejectAccepted):
            return replace(accepted, already_accepted=True)
        _fail(ActorGameStateCompletionFailureReason.COMPLETION_IDENTITY_CONFLICT)

    def _accept_applied(
        self,
        commit_ready: ControlTurnCommitReady,
        completion: ControlCompletionIdentity,
    ) -> tuple[ActorVisibleGameState, SnapshotVisibilityAccepted]:
        outcome = commit_ready.build_outcome
        if not isinstance(outcome, BuildPlanReady):
            _fail(ActorGameStateCompletionFailureReason.UNSUPPORTED_BUILD_OUTCOME)
        plan = outcome.plan
        receipt = commit_ready.accepted_receipt
        candidate = plan.candidate_snapshot
        if not isinstance(candidate, CandidateGameSnapshot):
            _fail(
                ActorGameStateCompletionFailureReason.SNAPSHOT_IDENTITY_MISMATCH
            )
        current_state = self._cell.state
        current = current_state.snapshot
        _validate_scope_and_operation(current, plan, receipt)
        if plan.expected_state_version != current.state_version:
            _fail(ActorGameStateCompletionFailureReason.VERSION_MISMATCH)
        if (
            candidate.state_version != current.state_version + 1
            or receipt.committed_state_version != candidate.state_version
        ):
            _fail(ActorGameStateCompletionFailureReason.VERSION_MISMATCH)
        if (
            plan.expected_cursor != current_state.committed_control_cursor
            or plan.input_sequence_no != current_state.committed_control_cursor + 1
            or receipt.committed_cursor != plan.input_sequence_no
        ):
            _fail(ActorGameStateCompletionFailureReason.CONTROL_CURSOR_MISMATCH)
        if candidate.last_applied_sequence_no != plan.input_sequence_no:
            _fail(
                ActorGameStateCompletionFailureReason.MATERIALIZATION_CURSOR_MISMATCH
            )
        _validate_candidate_scope(current, candidate)
        _validate_result_event_references(plan.result_events, receipt)
        _validate_ownership(
            current_state.ownership_generation,
            plan.ownership_intent.intent_type,
            plan.ownership_intent.expected_generation,
            plan.ownership_intent.resulting_generation,
            receipt.ownership_generation,
        )

        accepted = SnapshotVisibilityAccepted(
            game_id=plan.game_id,
            session_id=plan.session_id,
            operation_id=plan.operation_id,
            command_id=plan.command_id,
            input_event_id=plan.input_event_id,
            input_sequence_no=plan.input_sequence_no,
            previous_state_version=current.state_version,
            committed_state_version=candidate.state_version,
            previous_cursor=current_state.committed_control_cursor,
            committed_cursor=receipt.committed_cursor,
            previous_snapshot_identity=LifecycleSnapshotIdentity.from_snapshot(
                current
            ),
            committed_snapshot_identity=LifecycleSnapshotIdentity.from_snapshot(
                candidate
            ),
            ownership_generation=receipt.ownership_generation,
            commit_evidence_reference=receipt.commit_evidence_reference,
            already_visible=False,
        )
        return (
            ActorVisibleGameState(
                snapshot=candidate,
                committed_control_cursor=receipt.committed_cursor,
                ownership_generation=receipt.ownership_generation,
                last_completion_identity=completion,
            ),
            accepted,
        )

    def _accept_rejected(
        self,
        commit_ready: ControlTurnCommitReady,
        completion: ControlCompletionIdentity,
    ) -> tuple[ActorVisibleGameState, CommittedControlRejectAccepted]:
        outcome = commit_ready.build_outcome
        if not isinstance(outcome, BuildReject):
            _fail(ActorGameStateCompletionFailureReason.UNSUPPORTED_BUILD_OUTCOME)
        plan = outcome.plan
        receipt = commit_ready.accepted_receipt
        current_state = self._cell.state
        current = current_state.snapshot
        _validate_scope_and_operation(current, plan, receipt)
        if (
            plan.expected_state_version != current.state_version
            or receipt.committed_state_version != current.state_version
        ):
            _fail(ActorGameStateCompletionFailureReason.VERSION_MISMATCH)
        if (
            plan.expected_cursor != current_state.committed_control_cursor
            or plan.input_sequence_no != current_state.committed_control_cursor + 1
            or receipt.committed_cursor != plan.input_sequence_no
        ):
            _fail(ActorGameStateCompletionFailureReason.CONTROL_CURSOR_MISMATCH)
        if receipt.ownership_generation != current_state.ownership_generation:
            _fail(ActorGameStateCompletionFailureReason.OWNERSHIP_MISMATCH)
        try:
            payload = validate_control_result_event(plan.rejection_event)
        except (TypeError, ValueError):
            _fail(
                ActorGameStateCompletionFailureReason.RECEIPT_BINDING_MISMATCH
            )
        if (
            plan.rejection_event.event_type
            is not GameEventType.SESSION_CONTROL_REJECTED
            or payload.command_id != plan.command_id
            or payload.operation_id != plan.operation_id
            or payload.input_event_id != plan.input_event_id
            or payload.result_state_version != current.state_version
            or getattr(payload, "state_version", None) != current.state_version
            or not isinstance(getattr(payload, "reason_code", None), str)
            or not payload.reason_code.strip()
            or payload.result_code != payload.reason_code
        ):
            _fail(
                ActorGameStateCompletionFailureReason.RECEIPT_BINDING_MISMATCH
            )
        _validate_result_event_references((plan.rejection_event,), receipt)
        reference = receipt.result_event_references[0]
        accepted = CommittedControlRejectAccepted(
            game_id=plan.game_id,
            session_id=plan.session_id,
            command_id=plan.command_id,
            operation_id=plan.operation_id,
            claim_id=plan.operation_claim_id,
            input_event_id=plan.input_event_id,
            input_sequence_no=plan.input_sequence_no,
            unchanged_state_version=current.state_version,
            previous_cursor=current_state.committed_control_cursor,
            committed_cursor=receipt.committed_cursor,
            rejection_reason=payload.reason_code,
            committed_rejection_event_reference=reference.stored_event_reference,
            commit_evidence_reference=receipt.commit_evidence_reference,
            already_accepted=False,
        )
        return (
            ActorVisibleGameState(
                snapshot=current,
                committed_control_cursor=receipt.committed_cursor,
                ownership_generation=current_state.ownership_generation,
                last_completion_identity=completion,
            ),
            accepted,
        )


def _completion_identity(
    commit_ready: ControlTurnCommitReady,
) -> ControlCompletionIdentity:
    outcome = commit_ready.build_outcome
    if not isinstance(outcome, (BuildPlanReady, BuildReject)):
        _fail(ActorGameStateCompletionFailureReason.UNSUPPORTED_BUILD_OUTCOME)
    plan = outcome.plan
    receipt = commit_ready.accepted_receipt
    return ControlCompletionIdentity(
        game_id=plan.game_id,
        session_id=plan.session_id,
        command_id=plan.command_id,
        operation_id=plan.operation_id,
        input_event_id=plan.input_event_id,
        input_sequence_no=plan.input_sequence_no,
        state_version=receipt.committed_state_version,
        commit_evidence_reference=receipt.commit_evidence_reference,
        completion_kind=(
            ControlCompletionKind.APPLIED
            if isinstance(outcome, BuildPlanReady)
            else ControlCompletionKind.REJECTED
        ),
    )


def _validate_scope_and_operation(
    current: CandidateGameSnapshot,
    plan: ControlApplyPlan | ControlRejectPlan,
    receipt: ReceiptAccepted,
) -> None:
    expected_scope = (current.game_id, current.session_id)
    if (
        (plan.game_id, plan.session_id) != expected_scope
        or (receipt.game_id, receipt.session_id) != expected_scope
        or plan.group_id != current.group_id
    ):
        _fail(ActorGameStateCompletionFailureReason.SCOPE_MISMATCH)
    expected_identity = (
        plan.command_id,
        plan.operation_id,
        plan.operation_claim_id,
        plan.input_event_id,
        plan.input_sequence_no,
    )
    actual_identity = (
        receipt.command_id,
        receipt.operation_id,
        receipt.claim_id,
        receipt.input_event_id,
        receipt.input_sequence_no,
    )
    if actual_identity != expected_identity:
        _fail(
            ActorGameStateCompletionFailureReason.OPERATION_IDENTITY_MISMATCH
        )


def _validate_candidate_scope(
    current: CandidateGameSnapshot,
    candidate: CandidateGameSnapshot,
) -> None:
    if (
        candidate.game_id,
        candidate.session_id,
        candidate.group_id,
        candidate.dm_participant_id,
        candidate.snapshot_schema_version,
    ) != (
        current.game_id,
        current.session_id,
        current.group_id,
        current.dm_participant_id,
        current.snapshot_schema_version,
    ):
        _fail(ActorGameStateCompletionFailureReason.SNAPSHOT_IDENTITY_MISMATCH)


def _validate_result_event_references(
    events: tuple[GameEvent, ...],
    receipt: ReceiptAccepted,
) -> None:
    references = receipt.result_event_references
    if len(references) != len(events) or any(
        reference.event_id != event.event_id
        or reference.event_type is not event.event_type
        or reference.stored_event_reference != event.event_id
        for reference, event in zip(references, events)
    ):
        _fail(ActorGameStateCompletionFailureReason.RECEIPT_BINDING_MISMATCH)


def _validate_ownership(
    current: int | None,
    intent_type: OwnershipIntentType,
    expected: int | None,
    resulting: int | None,
    committed: int | None,
) -> None:
    valid = committed == resulting
    if intent_type is OwnershipIntentType.ACQUIRE:
        valid = (
            valid
            and current is None
            and expected is None
            and resulting is not None
        )
    elif intent_type is OwnershipIntentType.RETAIN:
        valid = valid and current == expected == resulting and current is not None
    elif intent_type is OwnershipIntentType.RELEASE:
        valid = (
            valid
            and current == expected
            and current is not None
            and resulting is None
        )
    elif intent_type is OwnershipIntentType.UNCHANGED:
        valid = valid and current == expected == resulting
    else:
        valid = False
    if not valid:
        _fail(ActorGameStateCompletionFailureReason.OWNERSHIP_MISMATCH)


def _fail(reason: ActorGameStateCompletionFailureReason) -> None:
    raise ActorGameStateCompletionBoundaryError(reason)
