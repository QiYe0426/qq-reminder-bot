"""In-memory, single-writer GameSession actor skeleton."""

from __future__ import annotations

from collections import deque
from threading import RLock
from typing import Callable

from game_runtime.errors import EventSessionMismatch
from game_runtime.event import GameEvent
from game_runtime.session import GameSession
from game_runtime.session_control.control_turn_contract import (
    ActorControlCommitBoundary,
    ActorControlCommitBoundaryError,
    ActorControlRejectCompletionBoundary,
    ActorControlTurnEvidenceFactory,
    ActorControlTurnProcessor,
    ActorControlTurnValidationError,
    ControlTurnCommitReady,
)
from game_runtime.session_control.apply_plan_builder import BuildPlanReady, BuildReject
from game_runtime.session_control.control_reject_completion import (
    CommittedControlRejectAccepted,
    ControlRejectCompletionBoundaryError,
)
from game_runtime.session_control.coordinator_evidence import (
    ActorValidatedControlTurnEvidence,
)
from game_runtime.session_control.delivery import ControlEventDeliveryEnvelope
from game_runtime.session_control.lifecycle_snapshot_boundary import (
    SnapshotVisibilityAccepted,
)

StateUpdater = Callable[[GameSession, GameEvent], None]


class GameSessionActor:
    """Serializes events for exactly one game_id.

    P3-A drains an in-memory mailbox synchronously under a per-actor writer lock.
    It performs no I/O and owns no background thread or process.
    """

    def __init__(
        self,
        session: GameSession,
        *,
        state_updater: StateUpdater | None = None,
        control_turn_evidence_factory: ActorControlTurnEvidenceFactory | None = None,
        control_turn_processor: ActorControlTurnProcessor | None = None,
        control_commit_boundary: ActorControlCommitBoundary | None = None,
        control_reject_completion_boundary: (
            ActorControlRejectCompletionBoundary | None
        ) = None,
    ) -> None:
        control_dependencies = (
            control_turn_evidence_factory,
            control_turn_processor,
            control_commit_boundary,
            control_reject_completion_boundary,
        )
        if any(value is not None for value in control_dependencies) and not all(
            value is not None for value in control_dependencies
        ):
            raise TypeError("Control Turn dependencies must be configured together")
        if state_updater is not None and all(
            value is not None for value in control_dependencies
        ):
            raise ValueError(
                "state_updater cannot be combined with Control Turn integration"
            )
        if control_turn_evidence_factory is not None and not callable(
            getattr(control_turn_evidence_factory, "build", None)
        ):
            raise TypeError("control_turn_evidence_factory must define build")
        if control_turn_processor is not None and not callable(
            getattr(control_turn_processor, "process", None)
        ):
            raise TypeError("control_turn_processor must define process")
        if control_commit_boundary is not None and not callable(
            getattr(control_commit_boundary, "accept", None)
        ):
            raise TypeError("control_commit_boundary must define accept")
        if control_reject_completion_boundary is not None and not callable(
            getattr(control_reject_completion_boundary, "accept", None)
        ):
            raise TypeError("control_reject_completion_boundary must define accept")
        self._session = session
        self._state_updater = state_updater
        self._control_turn_evidence_factory = control_turn_evidence_factory
        self._control_turn_processor = control_turn_processor
        self._control_commit_boundary = control_commit_boundary
        self._control_reject_completion_boundary = control_reject_completion_boundary
        self._control_turn_active = False
        self._mailbox: deque[GameEvent] = deque()
        self._seen_event_ids: set[str] = set()
        self._processed_event_ids: list[str] = []
        self._writer_lock = RLock()

    @property
    def game_id(self) -> str:
        return self._session.game_id

    @property
    def session_id(self) -> str:
        return self._session.session_id

    @property
    def session(self) -> GameSession:
        return self._session

    @property
    def processed_event_ids(self) -> tuple[str, ...]:
        return tuple(self._processed_event_ids)

    @property
    def pending_count(self) -> int:
        return len(self._mailbox)

    def receive(self, event: GameEvent) -> bool:
        """Accept and serially process an event.

        Returns False for an already-seen event ID. Cross-session delivery is
        always rejected.
        """

        self._validate_event_scope(event)
        with self._writer_lock:
            if event.event_id in self._seen_event_ids:
                return False
            self._seen_event_ids.add(event.event_id)
            self._mailbox.append(event)
            self._drain_mailbox()
            return True

    async def handle_control_turn(
        self,
        envelope: ControlEventDeliveryEnvelope,
    ) -> None:
        """Run one Gate-owned Control Turn without using the sync mailbox."""

        if not isinstance(envelope, ControlEventDeliveryEnvelope):
            raise TypeError("envelope must be a ControlEventDeliveryEnvelope")
        factory = self._control_turn_evidence_factory
        processor = self._control_turn_processor
        commit_boundary = self._control_commit_boundary
        reject_boundary = self._control_reject_completion_boundary
        if (
            factory is None
            or processor is None
            or commit_boundary is None
            or reject_boundary is None
        ):
            raise ActorControlTurnValidationError(
                "Control Turn integration is not configured"
            )
        if self._control_turn_active:
            raise ActorControlTurnValidationError(
                "a Control Turn is already active for this Actor"
            )
        self._control_turn_active = True
        try:
            await self._handle_owned_control_turn(
                envelope,
                factory,
                processor,
                commit_boundary,
                reject_boundary,
            )
        finally:
            self._control_turn_active = False

    async def _handle_owned_control_turn(
        self,
        envelope: ControlEventDeliveryEnvelope,
        factory: ActorControlTurnEvidenceFactory,
        processor: ActorControlTurnProcessor,
        commit_boundary: ActorControlCommitBoundary,
        reject_boundary: ActorControlRejectCompletionBoundary,
    ) -> None:
        try:
            self._validate_event_scope(envelope.event)
        except EventSessionMismatch as exc:
            raise ActorControlTurnValidationError(str(exc)) from exc

        try:
            evidence = factory.build(session=self._session, envelope=envelope)
        except Exception as exc:
            raise ActorControlTurnValidationError(
                "Control Turn evidence could not be frozen"
            ) from exc
        if not isinstance(evidence, ActorValidatedControlTurnEvidence):
            raise ActorControlTurnValidationError(
                "evidence factory must return ActorValidatedControlTurnEvidence"
            )
        if evidence.envelope != envelope:
            raise ActorControlTurnValidationError(
                "validated evidence must bind to the delivered Envelope"
            )

        commit_ready = await processor.process(evidence)
        if not isinstance(commit_ready, ControlTurnCommitReady):
            raise ActorControlTurnValidationError(
                "processor must return ControlTurnCommitReady"
            )
        if not isinstance(commit_ready.build_outcome, (BuildPlanReady, BuildReject)):
            raise ActorControlTurnValidationError(
                "commit-ready contains an unsupported build outcome"
            )
        _validate_commit_ready_binding(evidence, commit_ready)
        outcome = commit_ready.build_outcome
        if isinstance(outcome, BuildPlanReady):
            try:
                acceptance = commit_boundary.accept(commit_ready)
            except ActorControlCommitBoundaryError:
                raise
            except Exception as exc:
                raise ActorControlCommitBoundaryError(
                    "Actor commit boundary rejected committed Control Turn evidence"
                ) from exc
            if not isinstance(acceptance, SnapshotVisibilityAccepted):
                raise ActorControlCommitBoundaryError(
                    "Actor commit boundary must return SnapshotVisibilityAccepted"
                )
            return
        if isinstance(outcome, BuildReject):
            try:
                acceptance = reject_boundary.accept(commit_ready)
            except ControlRejectCompletionBoundaryError:
                raise
            except Exception as exc:
                raise ActorControlCommitBoundaryError(
                    "Actor reject boundary rejected committed Control Turn evidence"
                ) from exc
            if not isinstance(acceptance, CommittedControlRejectAccepted):
                raise ActorControlCommitBoundaryError(
                    "Actor reject boundary must return "
                    "CommittedControlRejectAccepted"
                )
            return
        raise ActorControlTurnValidationError(
            "commit-ready contains an unsupported build outcome"
        )

    def process(self, event: GameEvent) -> None:
        """Process one event while preserving the actor's single-writer lock."""

        self._validate_event_scope(event)
        with self._writer_lock:
            self.update_state(event)
            self._processed_event_ids.append(event.event_id)

    def update_state(self, event: GameEvent) -> None:
        """Invoke the optional pure state-update hook.

        No game logic is provided in P3-A. Later phases may bind a domain
        handler without allowing event producers to mutate the session.
        """

        if self._state_updater is not None:
            self._state_updater(self._session, event)

    def _drain_mailbox(self) -> None:
        while self._mailbox:
            event = self._mailbox.popleft()
            try:
                self.process(event)
            except Exception:
                self._seen_event_ids.discard(event.event_id)
                raise

    def _validate_event_scope(self, event: GameEvent) -> None:
        if event.game_id != self.game_id or event.session_id != self.session_id:
            raise EventSessionMismatch(
                f"event scope ({event.game_id!r}, {event.session_id!r}) does not "
                f"match actor scope ({self.game_id!r}, {self.session_id!r})"
            )


def _validate_commit_ready_binding(
    evidence: ActorValidatedControlTurnEvidence,
    commit_ready: ControlTurnCommitReady,
) -> None:
    envelope = evidence.envelope
    plan = commit_ready.build_outcome.plan
    receipt = commit_ready.accepted_receipt
    if (
        receipt.game_id,
        receipt.session_id,
        receipt.command_id,
        receipt.operation_id,
        receipt.claim_id,
        receipt.input_event_id,
        receipt.input_sequence_no,
    ) != (
        envelope.event.game_id,
        envelope.event.session_id,
        envelope.command_id,
        envelope.operation_id,
        evidence.claim_attempt.claim_id,
        envelope.event.event_id,
        envelope.event_sequence_no,
    ):
        raise ActorControlTurnValidationError(
            "commit-ready proof does not bind to the current Control Turn"
        )
    if (
        plan.game_id,
        plan.session_id,
        plan.group_id,
        plan.command_id,
        plan.operation_id,
        plan.operation_claim_id,
        plan.input_event_id,
        plan.input_sequence_no,
        plan.expected_state_version,
        plan.expected_cursor,
        plan.expected_binding_version,
    ) != (
        envelope.event.game_id,
        envelope.event.session_id,
        evidence.session_view.group_id,
        envelope.command_id,
        envelope.operation_id,
        evidence.claim_attempt.claim_id,
        envelope.event.event_id,
        envelope.event_sequence_no,
        evidence.session_view.state_version,
        evidence.session_view.last_applied_sequence_no,
        envelope.requester_binding_version,
    ):
        raise ActorControlTurnValidationError(
            "build outcome does not bind to the current Actor evidence"
        )
