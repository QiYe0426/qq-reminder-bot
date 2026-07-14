"""In-memory GameSession aggregate for P3-A."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from game_runtime.errors import InvalidPhaseTransition, InvalidSessionTransition
from game_runtime.identity import DMIdentity
from game_runtime.participant import ParticipantReference


class GameSessionStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    ENDED = "ENDED"


class GamePhase(str, Enum):
    LOBBY = "LOBBY"
    INTRODUCTION = "INTRODUCTION"
    EXPLORATION = "EXPLORATION"
    DISCUSSION = "DISCUSSION"
    VOTING = "VOTING"
    ENDING = "ENDING"


_SESSION_TRANSITIONS: dict[GameSessionStatus, frozenset[GameSessionStatus]] = {
    GameSessionStatus.CREATED: frozenset(
        {GameSessionStatus.RUNNING, GameSessionStatus.ENDED}
    ),
    GameSessionStatus.RUNNING: frozenset(
        {GameSessionStatus.PAUSED, GameSessionStatus.ENDED}
    ),
    GameSessionStatus.PAUSED: frozenset(
        {GameSessionStatus.RUNNING, GameSessionStatus.ENDED}
    ),
    GameSessionStatus.ENDED: frozenset(),
}

_PHASE_TRANSITIONS: dict[GamePhase, frozenset[GamePhase]] = {
    GamePhase.LOBBY: frozenset({GamePhase.INTRODUCTION}),
    GamePhase.INTRODUCTION: frozenset({GamePhase.EXPLORATION}),
    GamePhase.EXPLORATION: frozenset({GamePhase.DISCUSSION}),
    GamePhase.DISCUSSION: frozenset({GamePhase.EXPLORATION, GamePhase.VOTING}),
    GamePhase.VOTING: frozenset({GamePhase.DISCUSSION, GamePhase.ENDING}),
    GamePhase.ENDING: frozenset(),
}


@dataclass(slots=True)
class GameSession:
    """Single-game aggregate with lifecycle and phase invariants."""

    game_id: str
    session_id: str
    group_id: str
    dm_identity: DMIdentity
    participant_references: tuple[ParticipantReference, ...] = field(
        default_factory=tuple
    )
    status: GameSessionStatus = GameSessionStatus.CREATED
    current_phase: GamePhase = GamePhase.LOBBY
    state_version: int = 0

    def __post_init__(self) -> None:
        if not self.game_id:
            raise ValueError("game_id must not be empty")
        if not self.session_id:
            raise ValueError("session_id must not be empty")
        if not self.group_id:
            raise ValueError("group_id must not be empty")
        if self.state_version < 0:
            raise ValueError("state_version must not be negative")
        self.participant_references = tuple(self.participant_references)
        self._validate_state_pair()

    @property
    def participant_reference(self) -> tuple[ParticipantReference, ...]:
        """Compatibility name for the session's participant references."""

        return self.participant_references

    def can_transition_to(self, target: GameSessionStatus) -> bool:
        return target in _SESSION_TRANSITIONS[self.status]

    def transition_to(self, target: GameSessionStatus) -> None:
        """Apply one legal lifecycle transition and advance state_version."""

        if not self.can_transition_to(target):
            raise InvalidSessionTransition(
                f"session transition {self.status.value} -> {target.value} is not allowed"
            )

        if self.status is GameSessionStatus.CREATED and target is GameSessionStatus.RUNNING:
            self.current_phase = GamePhase.INTRODUCTION
        elif target is GameSessionStatus.ENDED:
            self.current_phase = GamePhase.ENDING

        self.status = target
        self.state_version += 1
        self._validate_state_pair()

    def can_transition_phase_to(self, target: GamePhase) -> bool:
        return (
            self.status is GameSessionStatus.RUNNING
            and target in _PHASE_TRANSITIONS[self.current_phase]
        )

    def transition_phase_to(self, target: GamePhase) -> None:
        """Apply a legal game-phase transition while the session is running."""

        if not self.can_transition_phase_to(target):
            raise InvalidPhaseTransition(
                f"phase transition {self.current_phase.value} -> {target.value} "
                f"is not allowed while session is {self.status.value}"
            )
        self.current_phase = target
        self.state_version += 1

    def _validate_state_pair(self) -> None:
        if self.status is GameSessionStatus.CREATED and self.current_phase is not GamePhase.LOBBY:
            raise ValueError("CREATED session must be in LOBBY phase")
        if self.status is GameSessionStatus.RUNNING and self.current_phase is GamePhase.LOBBY:
            raise ValueError("RUNNING session cannot be in LOBBY phase")
        if self.status is GameSessionStatus.ENDED and self.current_phase is not GamePhase.ENDING:
            raise ValueError("ENDED session must be in ENDING phase")
