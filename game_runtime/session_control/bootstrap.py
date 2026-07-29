"""CREATE_SESSION bootstrap contracts without persistence or Actor runtime."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable
from uuid import uuid4

from game_runtime.event import (
    GameEvent,
    GameEventType,
    SessionCreatedPayload,
    validate_control_result_event,
)
from game_runtime.participant import ParticipantType
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control.apply_contract import (
    CandidateSessionSnapshot,
    CreateSessionWithEventPlan,
    OwnershipIntent,
    OwnershipIntentType,
    ParticipantMutation,
    ParticipantMutationType,
)
from game_runtime.session_control.commands import SessionCommandType
from game_runtime.session_control.operation import ControlOperationStatus


@dataclass(frozen=True, slots=True)
class ProposedSessionIds:
    game_id: str
    session_id: str

    def __post_init__(self) -> None:
        _require_text("game_id", self.game_id)
        _require_text("session_id", self.session_id)


class BootstrapIdFactory:
    """Runtime-owned ID source; command payloads cannot select these IDs."""

    def __init__(
        self,
        game_id_factory: Callable[[], str] | None = None,
        session_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._game_id_factory = game_id_factory or (
            lambda: f"game-{uuid4()}"
        )
        self._session_id_factory = session_id_factory or (
            lambda: f"session-{uuid4()}"
        )

    def generate(self) -> ProposedSessionIds:
        return ProposedSessionIds(
            game_id=self._game_id_factory(),
            session_id=self._session_id_factory(),
        )


@dataclass(frozen=True, slots=True)
class CreateSessionBootstrapContext:
    """Post-resolution, pre-reservation CREATE scope."""

    group_id: str
    requester: str
    command_id: str
    correlation_id: str
    requested_at: datetime
    proposed_game_id: str
    proposed_session_id: str

    def __post_init__(self) -> None:
        for name in (
            "group_id",
            "requester",
            "command_id",
            "correlation_id",
            "proposed_game_id",
            "proposed_session_id",
        ):
            _require_text(name, getattr(self, name))
        _require_time("requested_at", self.requested_at)


@dataclass(frozen=True, slots=True)
class CreationGuard:
    """Opaque reservation evidence bound to one group and proposed scope."""

    reservation_id: str
    group_id: str
    requester: str
    command_id: str
    game_id: str
    session_id: str
    acquired_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "reservation_id",
            "group_id",
            "requester",
            "command_id",
            "game_id",
            "session_id",
        ):
            _require_text(name, getattr(self, name))
        _require_time("acquired_at", self.acquired_at)


class CreationGuardReleaseReason(str, Enum):
    COMMITTED = "COMMITTED"
    ABORTED = "ABORTED"
    EXPIRED = "EXPIRED"


@runtime_checkable
class CreationGuardPort(Protocol):
    """Persistent group reservation contract; no lock implementation here."""

    async def acquire(
        self,
        context: CreateSessionBootstrapContext,
        *,
        reservation_id: str,
        acquired_at: datetime,
    ) -> CreationGuard:
        """Acquire once; repeated command_id must return the same Guard."""

    async def check(self, group_id: str) -> CreationGuard | None:
        """Return the active Guard for exactly one group."""

    async def release(
        self,
        guard: CreationGuard,
        *,
        reason: CreationGuardReleaseReason,
        released_at: datetime,
    ) -> None:
        """Release only the matching reservation evidence."""


class ProvisionalActorContractError(ValueError):
    """Raised when a provisional Actor receives anything except its CREATE."""


@dataclass(frozen=True, slots=True)
class ProvisionalActorContract:
    """Restricted pre-commit Actor binding, not an active Actor implementation."""

    guard: CreationGuard

    @property
    def game_id(self) -> str:
        return self.guard.game_id

    @property
    def session_id(self) -> str:
        return self.guard.session_id

    @property
    def group_id(self) -> str:
        return self.guard.group_id

    @property
    def command_id(self) -> str:
        return self.guard.command_id

    @property
    def registry_publishable(self) -> bool:
        return False

    @property
    def has_routing_ownership(self) -> bool:
        return False

    @property
    def allowed_command_type(self) -> SessionCommandType:
        return SessionCommandType.CREATE_SESSION

    def validate_event(self, event: GameEvent) -> None:
        """Validate one CREATE Event without queuing, applying, or persisting it."""

        from game_runtime.session_control.event_integration import (
            validate_dm_command_event,
        )

        if not isinstance(event, GameEvent):
            raise TypeError("event must be a GameEvent")
        if event.game_id != self.game_id or event.session_id != self.session_id:
            raise ProvisionalActorContractError(
                "CREATE Event scope does not match provisional Actor"
            )
        if event.event_type is not GameEventType.DM_COMMAND:
            raise ProvisionalActorContractError(
                "provisional Actor only accepts DM_COMMAND"
            )
        payload = validate_dm_command_event(event)
        if payload.command_id != self.command_id:
            raise ProvisionalActorContractError(
                "CREATE Event command does not match reservation"
            )
        if payload.command_type is not SessionCommandType.CREATE_SESSION:
            raise ProvisionalActorContractError(
                "provisional Actor only accepts CREATE_SESSION"
            )
        if payload.requester != self.guard.requester:
            raise ProvisionalActorContractError(
                "CREATE Event requester does not match reservation"
            )
        if payload.observed_state_version != 0:
            raise ProvisionalActorContractError(
                "CREATE Event observed_state_version must be zero"
            )


@dataclass(frozen=True, slots=True)
class CreateSessionBootstrapPlan:
    """Immutable input for the future create_session_with_event transaction."""

    guard: CreationGuard
    operation_id: str
    operation_claim_id: str
    candidate_snapshot: CandidateSessionSnapshot
    initial_dm_participant: ParticipantMutation
    input_event: GameEvent
    result_event: GameEvent
    ownership_intent: OwnershipIntent
    operation_terminal_state: ControlOperationStatus

    def __post_init__(self) -> None:
        if not isinstance(self.guard, CreationGuard):
            raise TypeError("guard must be a CreationGuard")
        _require_text("operation_id", self.operation_id)
        _require_text("operation_claim_id", self.operation_claim_id)
        if not isinstance(self.candidate_snapshot, CandidateSessionSnapshot):
            raise TypeError("candidate_snapshot must be a CandidateSessionSnapshot")
        if not isinstance(self.initial_dm_participant, ParticipantMutation):
            raise TypeError("initial_dm_participant must be a ParticipantMutation")
        if not isinstance(self.input_event, GameEvent):
            raise TypeError("input_event must be a GameEvent")
        if not isinstance(self.result_event, GameEvent):
            raise TypeError("result_event must be a GameEvent")
        if not isinstance(self.ownership_intent, OwnershipIntent):
            raise TypeError("ownership_intent must be an OwnershipIntent")
        if self.operation_terminal_state is not ControlOperationStatus.SUCCESS:
            raise ValueError("CREATE bootstrap terminal state must be SUCCESS")
        self._validate_candidate()
        self._validate_initial_dm()
        ProvisionalActorContract(self.guard).validate_event(self.input_event)
        self._validate_result_event()

    @property
    def game_id(self) -> str:
        return self.guard.game_id

    @property
    def session_id(self) -> str:
        return self.guard.session_id

    @property
    def group_id(self) -> str:
        return self.guard.group_id

    @property
    def command_id(self) -> str:
        return self.guard.command_id

    @property
    def input_event_id(self) -> str:
        return self.input_event.event_id

    def _validate_candidate(self) -> None:
        candidate = self.candidate_snapshot
        if (
            candidate.game_id != self.game_id
            or candidate.session_id != self.session_id
            or candidate.group_id != self.group_id
        ):
            raise ValueError("CREATE candidate scope does not match Guard")
        if (
            candidate.status is not GameSessionStatus.CREATED
            or candidate.current_phase is not GamePhase.LOBBY
            or candidate.state_version != 0
            or candidate.last_applied_sequence_no != 0
        ):
            raise ValueError("CREATE candidate must be initial CREATED/LOBBY")

    def _validate_initial_dm(self) -> None:
        mutation = self.initial_dm_participant
        if (
            mutation.mutation_type is not ParticipantMutationType.UPSERT
            or mutation.participant_type is not ParticipantType.DM
            or mutation.participant_id
            != self.candidate_snapshot.dm_participant_id
            or mutation.expected_binding_version is not None
            or mutation.resulting_binding_version != 1
        ):
            raise ValueError("CREATE requires initial DM binding version 1")

    def _validate_result_event(self) -> None:
        if self.result_event.event_type is not GameEventType.SESSION_CREATED:
            raise ValueError("CREATE result must be SESSION_CREATED")
        payload = validate_control_result_event(self.result_event)
        if not isinstance(payload, SessionCreatedPayload):
            raise ValueError("CREATE result payload does not match")
        if (
            self.result_event.game_id != self.game_id
            or self.result_event.session_id != self.session_id
            or self.result_event.causation_event_id != self.input_event_id
            or payload.command_id != self.command_id
            or payload.operation_id != self.operation_id
            or payload.input_event_id != self.input_event_id
            or payload.result_state_version != 0
            or self.result_event.observed_state_version != 0
            or self.result_event.correlation_id != self.input_event.correlation_id
        ):
            raise ValueError("CREATE result evidence does not match bootstrap Plan")
        if (
            self.ownership_intent.intent_type is not OwnershipIntentType.UNCHANGED
            or self.ownership_intent.expected_generation is not None
            or self.ownership_intent.resulting_generation is not None
        ):
            raise ValueError("CREATED Session cannot acquire routing ownership")
        if not isinstance(self, CreateSessionWithEventPlan):
            raise TypeError("bootstrap Plan does not satisfy Atomic Apply contract")


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} must not be empty")


def _require_time(name: str, value: object) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
