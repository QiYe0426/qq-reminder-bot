"""In-memory, single-writer GameSession actor skeleton."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Callable

from game_runtime.errors import EventSessionMismatch
from game_runtime.event import GameEvent
from game_runtime.identity import DMIdentity
from game_runtime.participant import ParticipantReference
from game_runtime.session import GameSession
from game_runtime.session_control.actor_visible_game_state import (
    ActorVisibleGameState,
)
from game_runtime.session_control.control_turn_contract import (
    ActorControlCommitBoundary,
    ActorControlCommitBoundaryError,
    ActorControlRejectCompletionBoundary,
    ActorGameStateCompletionBoundary,
    ActorGameStateControlTurnEvidenceFactory,
    ActorControlTurnEvidenceFactory,
    ActorControlTurnProcessor,
    ActorControlTurnValidationError,
    ControlTurnCommitReady,
)
from game_runtime.session_control.game_state_completion import (
    ActorOwnedGameStateCompletionBoundary,
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


@dataclass(frozen=True, slots=True)
class _LegacySessionProjectionSeed:
    dm_identity: DMIdentity
    participant_references: tuple[ParticipantReference, ...]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_session(cls, session: GameSession) -> _LegacySessionProjectionSeed:
        return cls(
            dm_identity=session.dm_identity,
            participant_references=tuple(session.participant_references),
            created_at=session.created_at,
            updated_at=session.updated_at,
        )


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
        self._game_state_evidence_factory: (
            ActorGameStateControlTurnEvidenceFactory | None
        ) = None
        self._game_state_completion_boundary: (
            ActorGameStateCompletionBoundary | None
        ) = None
        self._session_projection_seed: _LegacySessionProjectionSeed | None = None
        self._control_turn_active = False
        self._mailbox: deque[GameEvent] = deque()
        self._seen_event_ids: set[str] = set()
        self._processed_event_ids: list[str] = []
        self._writer_lock = RLock()

    @classmethod
    def for_composite_control(
        cls,
        *,
        session_projection_seed: GameSession,
        initial_state: ActorVisibleGameState,
        control_turn_evidence_factory: ActorGameStateControlTurnEvidenceFactory,
        control_turn_processor: ActorControlTurnProcessor,
    ) -> GameSessionActor:
        if not isinstance(session_projection_seed, GameSession):
            raise TypeError("session_projection_seed must be a GameSession")
        if not isinstance(initial_state, ActorVisibleGameState):
            raise TypeError("initial_state must be an ActorVisibleGameState")
        if not callable(getattr(control_turn_evidence_factory, "build", None)):
            raise TypeError("control_turn_evidence_factory must define build")
        if not callable(getattr(control_turn_processor, "process", None)):
            raise TypeError("control_turn_processor must define process")
        snapshot = initial_state.snapshot
        if (
            session_projection_seed.game_id,
            session_projection_seed.session_id,
            session_projection_seed.group_id,
            session_projection_seed.dm_identity.participant_id,
        ) != (
            snapshot.game_id,
            snapshot.session_id,
            snapshot.group_id,
            snapshot.dm_participant_id,
        ):
            raise ActorControlTurnValidationError(
                "Session projection seed does not match Actor-visible state"
            )

        actor = cls(session_projection_seed)
        actor._session_projection_seed = _LegacySessionProjectionSeed.from_session(
            session_projection_seed
        )
        actor._game_state_evidence_factory = control_turn_evidence_factory
        actor._control_turn_processor = control_turn_processor
        actor._game_state_completion_boundary = (
            ActorOwnedGameStateCompletionBoundary(initial_state=initial_state)
        )
        return actor

    @property
    def game_id(self) -> str:
        if self._game_state_completion_boundary is not None:
            return self.visible_game_state.snapshot.game_id
        return self._session.game_id

    @property
    def session_id(self) -> str:
        if self._game_state_completion_boundary is not None:
            return self.visible_game_state.snapshot.session_id
        return self._session.session_id

    @property
    def session(self) -> GameSession:
        boundary = self._game_state_completion_boundary
        if boundary is not None:
            seed = self._session_projection_seed
            if seed is None:
                raise ActorControlTurnValidationError(
                    "Composite Session projection is not configured"
                )
            state = boundary.current_state
            snapshot = state.snapshot
            return GameSession(
                game_id=snapshot.game_id,
                session_id=snapshot.session_id,
                group_id=snapshot.group_id,
                dm_identity=seed.dm_identity,
                participant_references=seed.participant_references,
                status=snapshot.status,
                current_phase=snapshot.current_phase,
                state_version=snapshot.state_version,
                last_applied_sequence_no=state.committed_control_cursor,
                created_at=seed.created_at,
                updated_at=seed.updated_at,
            )
        return self._session

    @property
    def visible_game_state(self) -> ActorVisibleGameState:
        boundary = self._game_state_completion_boundary
        if boundary is None:
            raise ActorControlTurnValidationError(
                "Actor-visible Game State is not configured"
            )
        return boundary.current_state

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
        if self._game_state_completion_boundary is not None:
            await self._handle_composite_control_turn(envelope)
            return
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

    async def _handle_composite_control_turn(
        self,
        envelope: ControlEventDeliveryEnvelope,
    ) -> None:
        factory = self._game_state_evidence_factory
        processor = self._control_turn_processor
        boundary = self._game_state_completion_boundary
        if factory is None or processor is None or boundary is None:
            raise ActorControlTurnValidationError(
                "Composite Control Turn integration is not configured"
            )
        if self._control_turn_active:
            raise ActorControlTurnValidationError(
                "a Control Turn is already active for this Actor"
            )
        self._control_turn_active = True
        try:
            try:
                self._validate_event_scope(envelope.event)
            except EventSessionMismatch as exc:
                raise ActorControlTurnValidationError(str(exc)) from exc
            state = boundary.current_state
            try:
                evidence = factory.build(state=state, envelope=envelope)
            except Exception as exc:
                raise ActorControlTurnValidationError(
                    "Control Turn evidence could not be frozen"
                ) from exc
            if not isinstance(evidence, ActorValidatedControlTurnEvidence):
                raise ActorControlTurnValidationError(
                    "evidence factory must return "
                    "ActorValidatedControlTurnEvidence"
                )
            if evidence.envelope != envelope:
                raise ActorControlTurnValidationError(
                    "validated evidence must bind to the delivered Envelope"
                )
            _validate_game_state_evidence_binding(state, evidence)
            commit_ready = await processor.process(evidence)
            if not isinstance(commit_ready, ControlTurnCommitReady):
                raise ActorControlTurnValidationError(
                    "processor must return ControlTurnCommitReady"
                )
            _validate_commit_ready_binding(evidence, commit_ready)
            try:
                acceptance = boundary.accept(commit_ready)
            except ActorControlCommitBoundaryError:
                raise
            except Exception as exc:
                raise ActorControlCommitBoundaryError(
                    "Actor Game State boundary rejected committed Control Turn "
                    "evidence"
                ) from exc
            outcome = commit_ready.build_outcome
            if isinstance(outcome, BuildPlanReady) and not isinstance(
                acceptance,
                SnapshotVisibilityAccepted,
            ):
                raise ActorControlCommitBoundaryError(
                    "Applied Control Turn must return SnapshotVisibilityAccepted"
                )
            if isinstance(outcome, BuildReject) and not isinstance(
                acceptance,
                CommittedControlRejectAccepted,
            ):
                raise ActorControlCommitBoundaryError(
                    "Rejected Control Turn must return "
                    "CommittedControlRejectAccepted"
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


def _validate_game_state_evidence_binding(
    state: ActorVisibleGameState,
    evidence: ActorValidatedControlTurnEvidence,
) -> None:
    snapshot = state.snapshot
    session = evidence.session_view
    ownership = evidence.ownership_evidence
    if session.current_game_snapshot is not snapshot:
        raise ActorControlTurnValidationError(
            "validated evidence must carry the exact Actor-visible Snapshot"
        )
    if (
        session.game_id,
        session.session_id,
        session.group_id,
        session.dm_participant_id,
        session.status,
        session.current_phase,
        session.state_version,
        session.last_applied_sequence_no,
    ) != (
        snapshot.game_id,
        snapshot.session_id,
        snapshot.group_id,
        snapshot.dm_participant_id,
        snapshot.status,
        snapshot.current_phase,
        snapshot.state_version,
        state.committed_control_cursor,
    ):
        raise ActorControlTurnValidationError(
            "validated Session evidence does not match Actor-visible state"
        )
    if (
        ownership.game_id,
        ownership.session_id,
        ownership.group_id,
        ownership.active_generation,
        ownership.observed_state_version,
    ) != (
        snapshot.game_id,
        snapshot.session_id,
        snapshot.group_id,
        state.ownership_generation,
        snapshot.state_version,
    ):
        raise ActorControlTurnValidationError(
            "validated ownership evidence does not match Actor-visible state"
        )
