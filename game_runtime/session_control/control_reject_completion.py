"""Actor-owned completion for one atomically committed Control rejection."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import TypeAlias

from game_runtime.event import GameEventType, validate_control_result_event
from game_runtime.session_control.apply_plan_builder import BuildReject
from game_runtime.session_control.control_turn_contract import (
    ActorControlCommitBoundaryError,
    ControlTurnCommitReady,
)
from game_runtime.session_control.lifecycle_snapshot_boundary import (
    SnapshotVisibilityAccepted,
)
from game_runtime.session_control.receipt_validation import ReceiptAccepted


class ControlRejectCompletionFailureReason(str, Enum):
    INVALID_COMMIT_READY = "INVALID_COMMIT_READY"
    BUILD_PLAN_NOT_REJECT = "BUILD_PLAN_NOT_REJECT"
    RECEIPT_INVALID = "RECEIPT_INVALID"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    REJECTION_REASON_MISMATCH = "REJECTION_REASON_MISMATCH"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    CURSOR_MISMATCH = "CURSOR_MISMATCH"
    RESULT_EVENT_MISMATCH = "RESULT_EVENT_MISMATCH"
    ALREADY_ACCEPTED_CONFLICT = "ALREADY_ACCEPTED_CONFLICT"


class ControlRejectCompletionBoundaryError(ActorControlCommitBoundaryError):
    """Typed fail-closed rejection from the committed-reject boundary."""

    def __init__(self, reason: ControlRejectCompletionFailureReason) -> None:
        if not isinstance(reason, ControlRejectCompletionFailureReason):
            raise TypeError("reason must be a ControlRejectCompletionFailureReason")
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True)
class CommittedControlRejectAccepted:
    """Value proof that one committed business rejection is Actor-accepted."""

    game_id: str
    session_id: str
    command_id: str
    operation_id: str
    claim_id: str
    input_event_id: str
    input_sequence_no: int
    unchanged_state_version: int
    previous_cursor: int
    committed_cursor: int
    rejection_reason: str
    committed_rejection_event_reference: str
    commit_evidence_reference: str
    already_accepted: bool

    def __post_init__(self) -> None:
        for name in (
            "game_id",
            "session_id",
            "command_id",
            "operation_id",
            "claim_id",
            "input_event_id",
            "rejection_reason",
            "committed_rejection_event_reference",
            "commit_evidence_reference",
        ):
            _require_text(name, getattr(self, name))
        _require_positive("input_sequence_no", self.input_sequence_no)
        _require_non_negative(
            "unchanged_state_version", self.unchanged_state_version
        )
        _require_non_negative("previous_cursor", self.previous_cursor)
        _require_positive("committed_cursor", self.committed_cursor)
        if self.committed_cursor != self.input_sequence_no:
            raise ValueError("committed_cursor must equal input_sequence_no")
        if self.committed_cursor != self.previous_cursor + 1:
            raise ValueError("committed_cursor must advance previous_cursor once")
        if not isinstance(self.already_accepted, bool):
            raise TypeError("already_accepted must be a bool")


ControlTurnAcceptance: TypeAlias = (
    SnapshotVisibilityAccepted | CommittedControlRejectAccepted
)


class ActorOwnedControlRejectCompletionBoundary:
    """Accept committed BuildReject proof without replacing lifecycle Snapshot."""

    __slots__ = (
        "_accepted_by_operation",
        "_current_cursor",
        "_current_state_version",
        "_game_id",
        "_session_id",
    )

    def __init__(
        self,
        *,
        game_id: str,
        session_id: str,
        current_state_version: int,
        current_cursor: int,
    ) -> None:
        _require_text("game_id", game_id)
        _require_text("session_id", session_id)
        _require_non_negative("current_state_version", current_state_version)
        _require_non_negative("current_cursor", current_cursor)
        self._game_id = game_id
        self._session_id = session_id
        self._current_state_version = current_state_version
        self._current_cursor = current_cursor
        self._accepted_by_operation: dict[
            str, CommittedControlRejectAccepted
        ] = {}

    @property
    def current_state_version(self) -> int:
        return self._current_state_version

    @property
    def current_cursor(self) -> int:
        return self._current_cursor

    def accept(
        self,
        commit_ready: ControlTurnCommitReady,
    ) -> CommittedControlRejectAccepted:
        if not isinstance(commit_ready, ControlTurnCommitReady):
            _fail(ControlRejectCompletionFailureReason.INVALID_COMMIT_READY)
        if not isinstance(commit_ready.build_outcome, BuildReject):
            _fail(ControlRejectCompletionFailureReason.BUILD_PLAN_NOT_REJECT)
        if not isinstance(commit_ready.accepted_receipt, ReceiptAccepted):
            _fail(ControlRejectCompletionFailureReason.RECEIPT_INVALID)

        plan = commit_ready.build_outcome.plan
        receipt = commit_ready.accepted_receipt
        if (
            plan.game_id,
            plan.session_id,
            receipt.game_id,
            receipt.session_id,
        ) != (self._game_id, self._session_id) * 2:
            _fail(ControlRejectCompletionFailureReason.SCOPE_MISMATCH)
        if (
            receipt.command_id,
            receipt.operation_id,
            receipt.claim_id,
            receipt.input_event_id,
            receipt.input_sequence_no,
        ) != (
            plan.command_id,
            plan.operation_id,
            plan.operation_claim_id,
            plan.input_event_id,
            plan.input_sequence_no,
        ):
            _fail(ControlRejectCompletionFailureReason.IDENTITY_MISMATCH)

        rejection_event = plan.rejection_event
        try:
            payload = validate_control_result_event(rejection_event)
        except (TypeError, ValueError):
            _fail(ControlRejectCompletionFailureReason.REJECTION_REASON_MISMATCH)
        if (
            rejection_event.event_type is not GameEventType.SESSION_CONTROL_REJECTED
            or not isinstance(getattr(payload, "reason_code", None), str)
            or not payload.reason_code.strip()
            or payload.result_code != payload.reason_code
            or payload.command_id != plan.command_id
            or payload.operation_id != plan.operation_id
            or payload.input_event_id != plan.input_event_id
        ):
            _fail(ControlRejectCompletionFailureReason.REJECTION_REASON_MISMATCH)

        if (
            receipt.committed_state_version != plan.expected_state_version
            or payload.result_state_version != plan.expected_state_version
            or getattr(payload, "state_version", None) != plan.expected_state_version
            or plan.expected_state_version != self._current_state_version
        ):
            _fail(ControlRejectCompletionFailureReason.VERSION_MISMATCH)
        if (
            receipt.committed_cursor != receipt.input_sequence_no
            or receipt.committed_cursor != plan.input_sequence_no
            or plan.input_sequence_no != plan.expected_cursor + 1
        ):
            _fail(ControlRejectCompletionFailureReason.CURSOR_MISMATCH)

        references = receipt.result_event_references
        if (
            len(references) != 1
            or references[0].event_id != rejection_event.event_id
            or references[0].event_type is not rejection_event.event_type
            or references[0].stored_event_reference != rejection_event.event_id
            or references[0].sequence_no <= plan.input_sequence_no
        ):
            _fail(ControlRejectCompletionFailureReason.RESULT_EVENT_MISMATCH)
        accepted = CommittedControlRejectAccepted(
            game_id=plan.game_id,
            session_id=plan.session_id,
            command_id=plan.command_id,
            operation_id=plan.operation_id,
            claim_id=plan.operation_claim_id,
            input_event_id=plan.input_event_id,
            input_sequence_no=plan.input_sequence_no,
            unchanged_state_version=plan.expected_state_version,
            previous_cursor=plan.expected_cursor,
            committed_cursor=receipt.committed_cursor,
            rejection_reason=payload.reason_code,
            committed_rejection_event_reference=references[0].stored_event_reference,
            commit_evidence_reference=receipt.commit_evidence_reference,
            already_accepted=False,
        )

        existing = self._accepted_by_operation.get(plan.operation_id)
        if existing is not None:
            if _acceptance_identity(existing) != _acceptance_identity(accepted):
                _fail(
                    ControlRejectCompletionFailureReason.ALREADY_ACCEPTED_CONFLICT
                )
            return replace(existing, already_accepted=True)
        if plan.expected_cursor != self._current_cursor:
            _fail(ControlRejectCompletionFailureReason.CURSOR_MISMATCH)

        self._accepted_by_operation[plan.operation_id] = accepted
        self._current_cursor = accepted.committed_cursor
        return accepted


def _acceptance_identity(
    accepted: CommittedControlRejectAccepted,
) -> tuple[object, ...]:
    return (
        accepted.game_id,
        accepted.session_id,
        accepted.command_id,
        accepted.operation_id,
        accepted.claim_id,
        accepted.input_event_id,
        accepted.input_sequence_no,
        accepted.unchanged_state_version,
        accepted.previous_cursor,
        accepted.committed_cursor,
        accepted.rejection_reason,
        accepted.committed_rejection_event_reference,
        accepted.commit_evidence_reference,
    )


def _fail(reason: ControlRejectCompletionFailureReason) -> None:
    raise ControlRejectCompletionBoundaryError(reason)


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")


def _require_non_negative(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _require_positive(name: str, value: object) -> None:
    _require_non_negative(name, value)
    if value == 0:
        raise ValueError(f"{name} must be a positive integer")
