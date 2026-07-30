"""Immutable contracts for one Actor-owned Session Control turn."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from game_runtime.session import GameSession
from game_runtime.session_control.apply_plan_builder import BuildPlanReady, BuildReject
from game_runtime.session_control.actor_visible_game_state import ActorVisibleGameState
from game_runtime.session_control.coordinator_evidence import (
    ActorValidatedControlTurnEvidence,
)
from game_runtime.session_control.delivery import ControlEventDeliveryEnvelope
from game_runtime.session_control.receipt_validation import ReceiptAccepted

if TYPE_CHECKING:
    from game_runtime.session_control.control_reject_completion import (
        CommittedControlRejectAccepted,
    )
    from game_runtime.session_control.lifecycle_snapshot_boundary import (
        SnapshotVisibilityAccepted,
    )


class ActorControlTurnError(RuntimeError):
    """Base error for an Actor-owned Control Turn that cannot complete."""


class ActorControlTurnValidationError(ActorControlTurnError):
    """The Actor cannot freeze or bind the delivered Control Turn."""


class ActorControlCommitBoundaryError(ActorControlTurnError):
    """A committed turn could not be accepted by the Actor boundary."""


class ControlTurnFailureReason(str, Enum):
    CLAIM_CONFLICT = "CLAIM_CONFLICT"
    CLAIM_UNKNOWN = "CLAIM_UNKNOWN"
    BUILD_NON_COMMIT = "BUILD_NON_COMMIT"
    APPLY_CONFLICT = "APPLY_CONFLICT"
    APPLY_UNKNOWN = "APPLY_UNKNOWN"
    RECEIPT_INVALID = "RECEIPT_INVALID"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class ControlTurnProcessingError(ActorControlTurnError):
    """Fail-closed classification of a non-committable Coordinator outcome."""

    def __init__(self, reason: ControlTurnFailureReason) -> None:
        if not isinstance(reason, ControlTurnFailureReason):
            raise TypeError("reason must be a ControlTurnFailureReason")
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True)
class ControlTurnCommitReady:
    """Validated proof ready for the separate Actor commit boundary."""

    build_outcome: BuildPlanReady | BuildReject
    accepted_receipt: ReceiptAccepted

    def __post_init__(self) -> None:
        if not isinstance(self.build_outcome, (BuildPlanReady, BuildReject)):
            raise TypeError("build_outcome must be BuildPlanReady or BuildReject")
        if not isinstance(self.accepted_receipt, ReceiptAccepted):
            raise TypeError("accepted_receipt must be a ReceiptAccepted")
        plan = self.build_outcome.plan
        receipt = self.accepted_receipt
        if (
            receipt.game_id,
            receipt.session_id,
            receipt.command_id,
            receipt.operation_id,
            receipt.claim_id,
            receipt.input_event_id,
            receipt.input_sequence_no,
        ) != (
            plan.game_id,
            plan.session_id,
            plan.command_id,
            plan.operation_id,
            plan.operation_claim_id,
            plan.input_event_id,
            plan.input_sequence_no,
        ):
            raise ValueError("accepted Receipt does not bind to the build outcome")

        if isinstance(self.build_outcome, BuildPlanReady):
            expected_version = plan.candidate_snapshot.state_version
            expected_events = plan.result_events
            expected_ownership = plan.ownership_intent.resulting_generation
            if receipt.ownership_generation != expected_ownership:
                raise ValueError("accepted Receipt ownership does not match the plan")
        else:
            expected_version = plan.expected_state_version
            expected_events = (plan.rejection_event,)
        if receipt.committed_state_version != expected_version:
            raise ValueError("accepted Receipt version does not match the plan")
        references = receipt.result_event_references
        if len(references) != len(expected_events) or any(
            reference.event_id != event.event_id
            or reference.event_type is not event.event_type
            or reference.stored_event_reference != event.event_id
            for reference, event in zip(references, expected_events)
        ):
            raise ValueError("accepted Receipt Result Events do not match the plan")


@runtime_checkable
class ActorControlTurnEvidenceFactory(Protocol):
    """Synchronously freeze Actor-owned values for one Control Turn."""

    def build(
        self,
        *,
        session: GameSession,
        envelope: ControlEventDeliveryEnvelope,
    ) -> ActorValidatedControlTurnEvidence:
        """Return immutable evidence without I/O or Session mutation."""


@runtime_checkable
class ActorGameStateControlTurnEvidenceFactory(Protocol):
    """Synchronously freeze one Control Turn from the immutable Actor state."""

    def build(
        self,
        *,
        state: ActorVisibleGameState,
        envelope: ControlEventDeliveryEnvelope,
    ) -> ActorValidatedControlTurnEvidence:
        """Return immutable evidence without reading a mutable Session."""


@runtime_checkable
class ActorControlTurnProcessor(Protocol):
    """Convert validated evidence into commit-ready proof or fail closed."""

    async def process(
        self,
        evidence: ActorValidatedControlTurnEvidence,
    ) -> ControlTurnCommitReady:
        """Await the frozen Coordinator chain exactly once."""


@runtime_checkable
class ActorControlCommitBoundary(Protocol):
    """Accept commit-ready proof while preserving Actor ownership."""

    def accept(
        self,
        commit_ready: ControlTurnCommitReady,
    ) -> SnapshotVisibilityAccepted:
        """Synchronously publish one proven committed Snapshot."""


@runtime_checkable
class ActorControlRejectCompletionBoundary(Protocol):
    """Accept one proven committed business rejection without Snapshot apply."""

    def accept(
        self,
        commit_ready: ControlTurnCommitReady,
    ) -> CommittedControlRejectAccepted:
        """Synchronously accept one committed rejection proof."""


@runtime_checkable
class ActorGameStateCompletionBoundary(Protocol):
    """Own the single Actor-visible state cell for both terminal outcomes."""

    @property
    def current_state(self) -> ActorVisibleGameState:
        """Return the current immutable Actor-visible state."""

    def accept(
        self,
        commit_ready: ControlTurnCommitReady,
    ) -> SnapshotVisibilityAccepted | CommittedControlRejectAccepted:
        """Synchronously publish one committed outcome into the state cell."""
