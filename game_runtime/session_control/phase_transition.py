"""Pure, deterministic game-phase transition contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from game_runtime.session import GamePhase


class PhaseTransitionRejectReason(str, Enum):
    INVALID_PHASE_TRANSITION = "INVALID_PHASE_TRANSITION"


_LEGAL_EDGES: frozenset[tuple[GamePhase, GamePhase]] = frozenset(
    {
        (GamePhase.INTRODUCTION, GamePhase.EXPLORATION),
        (GamePhase.EXPLORATION, GamePhase.DISCUSSION),
        (GamePhase.DISCUSSION, GamePhase.EXPLORATION),
        (GamePhase.DISCUSSION, GamePhase.VOTING),
        (GamePhase.VOTING, GamePhase.DISCUSSION),
        (GamePhase.VOTING, GamePhase.ENDING),
    }
)


@dataclass(frozen=True, slots=True)
class PhaseTransitionRequest:
    current_phase: GamePhase
    target_phase: GamePhase

    def __post_init__(self) -> None:
        _require_game_phase("current_phase", self.current_phase)
        _require_game_phase("target_phase", self.target_phase)


@dataclass(frozen=True, slots=True)
class PhaseTransitionAccepted:
    previous_phase: GamePhase
    resulting_phase: GamePhase

    def __post_init__(self) -> None:
        _require_game_phase("previous_phase", self.previous_phase)
        _require_game_phase("resulting_phase", self.resulting_phase)


@dataclass(frozen=True, slots=True)
class PhaseTransitionRejected:
    current_phase: GamePhase
    requested_phase: GamePhase
    reason: PhaseTransitionRejectReason

    def __post_init__(self) -> None:
        _require_game_phase("current_phase", self.current_phase)
        _require_game_phase("requested_phase", self.requested_phase)
        if not isinstance(self.reason, PhaseTransitionRejectReason):
            raise TypeError("reason must be a PhaseTransitionRejectReason")


PhaseTransitionDecision = PhaseTransitionAccepted | PhaseTransitionRejected


def transition_phase(request: PhaseTransitionRequest) -> PhaseTransitionDecision:
    """Return the value-only decision for one requested phase change."""

    if not isinstance(request, PhaseTransitionRequest):
        raise TypeError("request must be a PhaseTransitionRequest")
    if (request.current_phase, request.target_phase) in _LEGAL_EDGES:
        return PhaseTransitionAccepted(
            previous_phase=request.current_phase,
            resulting_phase=request.target_phase,
        )
    return PhaseTransitionRejected(
        current_phase=request.current_phase,
        requested_phase=request.target_phase,
        reason=PhaseTransitionRejectReason.INVALID_PHASE_TRANSITION,
    )


def _require_game_phase(name: str, value: object) -> None:
    if not isinstance(value, GamePhase):
        raise TypeError(f"{name} must be a GamePhase")
