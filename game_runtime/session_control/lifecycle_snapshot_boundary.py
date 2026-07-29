"""Actor-owned visibility boundary for committed lifecycle snapshots."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control.apply_contract import (
    CandidateSessionSnapshot,
    OwnershipIntentType,
)
from game_runtime.session_control.apply_plan_builder import BuildPlanReady
from game_runtime.session_control.control_turn_contract import (
    ActorControlCommitBoundaryError,
    ControlTurnCommitReady,
)


class SnapshotVisibilityFailureReason(str, Enum):
    INVALID_COMMIT_READY = "INVALID_COMMIT_READY"
    BUILD_REJECT_NOT_REPLACEABLE = "BUILD_REJECT_NOT_REPLACEABLE"
    RECEIPT_BINDING_MISMATCH = "RECEIPT_BINDING_MISMATCH"
    OPERATION_IDENTITY_MISMATCH = "OPERATION_IDENTITY_MISMATCH"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    CURSOR_MISMATCH = "CURSOR_MISMATCH"
    OWNERSHIP_MISMATCH = "OWNERSHIP_MISMATCH"
    SNAPSHOT_IDENTITY_MISMATCH = "SNAPSHOT_IDENTITY_MISMATCH"
    ALREADY_VISIBLE_CONFLICT = "ALREADY_VISIBLE_CONFLICT"
    SNAPSHOT_REPLACEMENT_FAILED = "SNAPSHOT_REPLACEMENT_FAILED"


class SnapshotVisibilityBoundaryError(ActorControlCommitBoundaryError):
    """Typed fail-closed rejection from the Snapshot visibility boundary."""

    def __init__(self, reason: SnapshotVisibilityFailureReason) -> None:
        if not isinstance(reason, SnapshotVisibilityFailureReason):
            raise TypeError("reason must be a SnapshotVisibilityFailureReason")
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True)
class LifecycleSnapshotIdentity:
    game_id: str
    session_id: str
    group_id: str
    dm_participant_id: str
    lifecycle_status: GameSessionStatus
    current_phase: GamePhase
    state_version: int
    last_applied_sequence_no: int

    def __post_init__(self) -> None:
        for name in ("game_id", "session_id", "group_id", "dm_participant_id"):
            _require_text(name, getattr(self, name))
        if not isinstance(self.lifecycle_status, GameSessionStatus):
            raise TypeError("lifecycle_status must be a GameSessionStatus")
        if not isinstance(self.current_phase, GamePhase):
            raise TypeError("current_phase must be a GamePhase")
        _require_non_negative("state_version", self.state_version)
        _require_non_negative(
            "last_applied_sequence_no", self.last_applied_sequence_no
        )

    @classmethod
    def from_snapshot(
        cls,
        snapshot: CandidateSessionSnapshot,
    ) -> LifecycleSnapshotIdentity:
        if not isinstance(snapshot, CandidateSessionSnapshot):
            raise TypeError("snapshot must be a CandidateSessionSnapshot")
        return cls(
            game_id=snapshot.game_id,
            session_id=snapshot.session_id,
            group_id=snapshot.group_id,
            dm_participant_id=snapshot.dm_participant_id,
            lifecycle_status=snapshot.status,
            current_phase=snapshot.current_phase,
            state_version=snapshot.state_version,
            last_applied_sequence_no=snapshot.last_applied_sequence_no,
        )


@dataclass(frozen=True, slots=True)
class SnapshotVisibilityAccepted:
    game_id: str
    session_id: str
    operation_id: str
    command_id: str
    input_event_id: str
    input_sequence_no: int
    previous_state_version: int
    committed_state_version: int
    previous_cursor: int
    committed_cursor: int
    previous_snapshot_identity: LifecycleSnapshotIdentity
    committed_snapshot_identity: LifecycleSnapshotIdentity
    ownership_generation: int | None
    commit_evidence_reference: str
    already_visible: bool

    def __post_init__(self) -> None:
        for name in (
            "game_id",
            "session_id",
            "operation_id",
            "command_id",
            "input_event_id",
            "commit_evidence_reference",
        ):
            _require_text(name, getattr(self, name))
        _require_positive("input_sequence_no", self.input_sequence_no)
        for name in (
            "previous_state_version",
            "committed_state_version",
            "previous_cursor",
            "committed_cursor",
        ):
            _require_non_negative(name, getattr(self, name))
        if not isinstance(
            self.previous_snapshot_identity, LifecycleSnapshotIdentity
        ) or not isinstance(
            self.committed_snapshot_identity, LifecycleSnapshotIdentity
        ):
            raise TypeError("Snapshot identities must be LifecycleSnapshotIdentity")
        if self.ownership_generation is not None:
            _require_positive("ownership_generation", self.ownership_generation)
        if not isinstance(self.already_visible, bool):
            raise TypeError("already_visible must be a bool")


@dataclass(frozen=True, slots=True)
class _ActorVisibleLifecycleState:
    snapshot: CandidateSessionSnapshot
    ownership_generation: int | None
    operation_id: str | None = None
    command_id: str | None = None
    input_event_id: str | None = None
    commit_evidence_reference: str | None = None
    accepted: SnapshotVisibilityAccepted | None = None


class ActorOwnedLifecycleSnapshotCommitBoundary:
    """Synchronously publish one proven Candidate Snapshot to Actor visibility."""

    __slots__ = ("_visible_state",)

    def __init__(
        self,
        *,
        current_snapshot: CandidateSessionSnapshot,
        ownership_generation: int | None,
    ) -> None:
        if not isinstance(current_snapshot, CandidateSessionSnapshot):
            raise TypeError("current_snapshot must be a CandidateSessionSnapshot")
        if ownership_generation is not None:
            _require_positive("ownership_generation", ownership_generation)
        self._visible_state = _ActorVisibleLifecycleState(
            snapshot=current_snapshot,
            ownership_generation=ownership_generation,
        )

    @property
    def current_snapshot(self) -> CandidateSessionSnapshot:
        return self._visible_state.snapshot

    @property
    def ownership_generation(self) -> int | None:
        return self._visible_state.ownership_generation

    def accept(
        self,
        commit_ready: ControlTurnCommitReady,
    ) -> SnapshotVisibilityAccepted:
        if not isinstance(commit_ready, ControlTurnCommitReady):
            _fail(SnapshotVisibilityFailureReason.INVALID_COMMIT_READY)
        if not isinstance(commit_ready.build_outcome, BuildPlanReady):
            _fail(SnapshotVisibilityFailureReason.BUILD_REJECT_NOT_REPLACEABLE)

        plan = commit_ready.build_outcome.plan
        receipt = commit_ready.accepted_receipt
        candidate = plan.candidate_snapshot
        _validate_receipt_binding(plan, receipt)

        visible = self._visible_state
        candidate_identity = LifecycleSnapshotIdentity.from_snapshot(candidate)
        if visible.operation_id == plan.operation_id:
            if (
                visible.accepted is None
                or LifecycleSnapshotIdentity.from_snapshot(visible.snapshot)
                != candidate_identity
                or visible.command_id != plan.command_id
                or visible.input_event_id != plan.input_event_id
                or visible.commit_evidence_reference
                != receipt.commit_evidence_reference
                or visible.ownership_generation != receipt.ownership_generation
            ):
                _fail(SnapshotVisibilityFailureReason.ALREADY_VISIBLE_CONFLICT)
            return replace(visible.accepted, already_visible=True)

        current = visible.snapshot
        current_identity = LifecycleSnapshotIdentity.from_snapshot(current)
        if (
            current.game_id,
            current.session_id,
            current.group_id,
            current.dm_participant_id,
        ) != (
            plan.game_id,
            plan.session_id,
            plan.group_id,
            candidate.dm_participant_id,
        ):
            _fail(SnapshotVisibilityFailureReason.SNAPSHOT_IDENTITY_MISMATCH)
        if current.state_version != plan.expected_state_version:
            _fail(SnapshotVisibilityFailureReason.VERSION_MISMATCH)
        if (
            candidate.state_version != current.state_version + 1
            or receipt.committed_state_version != candidate.state_version
        ):
            _fail(SnapshotVisibilityFailureReason.VERSION_MISMATCH)
        if current.last_applied_sequence_no != plan.expected_cursor:
            _fail(SnapshotVisibilityFailureReason.CURSOR_MISMATCH)
        if (
            plan.input_sequence_no != current.last_applied_sequence_no + 1
            or candidate.last_applied_sequence_no != plan.input_sequence_no
            or receipt.committed_cursor != plan.input_sequence_no
        ):
            _fail(SnapshotVisibilityFailureReason.CURSOR_MISMATCH)
        _validate_ownership(
            visible.ownership_generation,
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
            previous_cursor=current.last_applied_sequence_no,
            committed_cursor=candidate.last_applied_sequence_no,
            previous_snapshot_identity=current_identity,
            committed_snapshot_identity=candidate_identity,
            ownership_generation=receipt.ownership_generation,
            commit_evidence_reference=receipt.commit_evidence_reference,
            already_visible=False,
        )
        replacement = _ActorVisibleLifecycleState(
            snapshot=candidate,
            ownership_generation=receipt.ownership_generation,
            operation_id=plan.operation_id,
            command_id=plan.command_id,
            input_event_id=plan.input_event_id,
            commit_evidence_reference=receipt.commit_evidence_reference,
            accepted=accepted,
        )
        try:
            self._visible_state = replacement
        except Exception as exc:
            raise SnapshotVisibilityBoundaryError(
                SnapshotVisibilityFailureReason.SNAPSHOT_REPLACEMENT_FAILED
            ) from exc
        if self._visible_state is not replacement:
            _fail(SnapshotVisibilityFailureReason.SNAPSHOT_REPLACEMENT_FAILED)
        return accepted


def _validate_receipt_binding(plan: object, receipt: object) -> None:
    expected = (
        plan.game_id,
        plan.session_id,
        plan.command_id,
        plan.operation_id,
        plan.operation_claim_id,
        plan.input_event_id,
        plan.input_sequence_no,
    )
    actual = (
        getattr(receipt, "game_id", None),
        getattr(receipt, "session_id", None),
        getattr(receipt, "command_id", None),
        getattr(receipt, "operation_id", None),
        getattr(receipt, "claim_id", None),
        getattr(receipt, "input_event_id", None),
        getattr(receipt, "input_sequence_no", None),
    )
    if actual[2:] != expected[2:]:
        _fail(SnapshotVisibilityFailureReason.OPERATION_IDENTITY_MISMATCH)
    if actual[:2] != expected[:2]:
        _fail(SnapshotVisibilityFailureReason.RECEIPT_BINDING_MISMATCH)
    expected_events = plan.result_events
    references = receipt.result_event_references
    if len(references) != len(expected_events) or any(
        reference.event_id != event.event_id
        or reference.event_type is not event.event_type
        or reference.stored_event_reference != event.event_id
        for reference, event in zip(references, expected_events)
    ):
        _fail(SnapshotVisibilityFailureReason.RECEIPT_BINDING_MISMATCH)
    if (
        receipt.committed_state_version != plan.candidate_snapshot.state_version
        or receipt.committed_cursor != plan.input_sequence_no
        or receipt.ownership_generation
        != plan.ownership_intent.resulting_generation
    ):
        _fail(SnapshotVisibilityFailureReason.RECEIPT_BINDING_MISMATCH)


def _validate_ownership(
    current: int | None,
    intent_type: OwnershipIntentType,
    expected: int | None,
    resulting: int | None,
    committed: int | None,
) -> None:
    valid = committed == resulting
    if intent_type is OwnershipIntentType.ACQUIRE:
        valid = valid and current is None and expected is None and resulting is not None
    elif intent_type is OwnershipIntentType.RETAIN:
        valid = valid and current == expected == resulting and current is not None
    elif intent_type is OwnershipIntentType.RELEASE:
        valid = valid and current == expected and current is not None and resulting is None
    elif intent_type is OwnershipIntentType.UNCHANGED:
        valid = valid and current == expected == resulting
    else:
        valid = False
    if not valid:
        _fail(SnapshotVisibilityFailureReason.OWNERSHIP_MISMATCH)


def _fail(reason: SnapshotVisibilityFailureReason) -> None:
    raise SnapshotVisibilityBoundaryError(reason)


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
