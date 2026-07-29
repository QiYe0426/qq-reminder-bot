"""Pure Start, Pause, and End lifecycle state transitions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control.commands import SessionCommandType


class LifecycleTransitionContractError(ValueError):
    """The transition request or value contract is malformed."""


class LifecycleTransitionPhaseEffect(str, Enum):
    START_BOOTSTRAP = "START_BOOTSTRAP"
    UNCHANGED = "UNCHANGED"
    END_TERMINAL = "END_TERMINAL"


class LifecycleTransitionRejectReason(str, Enum):
    INVALID_LIFECYCLE_TRANSITION = "INVALID_LIFECYCLE_TRANSITION"
    SESSION_ALREADY_ENDED = "SESSION_ALREADY_ENDED"


_SUPPORTED_OPERATIONS = frozenset(
    {
        SessionCommandType.START_GAME,
        SessionCommandType.PAUSE_GAME,
        SessionCommandType.END_GAME,
    }
)


@dataclass(frozen=True, slots=True)
class LifecycleTransitionState:
    lifecycle_status: GameSessionStatus
    phase: GamePhase

    def __post_init__(self) -> None:
        if not isinstance(self.lifecycle_status, GameSessionStatus):
            raise LifecycleTransitionContractError(
                "lifecycle_status must be a GameSessionStatus"
            )
        if not isinstance(self.phase, GamePhase):
            raise LifecycleTransitionContractError("phase must be a GamePhase")
        if (
            self.lifecycle_status is GameSessionStatus.CREATED
            and self.phase is not GamePhase.LOBBY
        ):
            raise LifecycleTransitionContractError(
                "CREATED lifecycle state must be in LOBBY"
            )
        if (
            self.lifecycle_status in {
                GameSessionStatus.RUNNING,
                GameSessionStatus.PAUSED,
            }
            and self.phase is GamePhase.LOBBY
        ):
            raise LifecycleTransitionContractError(
                "RUNNING or PAUSED lifecycle state cannot be in LOBBY"
            )
        if (
            self.lifecycle_status is GameSessionStatus.ENDED
            and self.phase is not GamePhase.ENDING
        ):
            raise LifecycleTransitionContractError(
                "ENDED lifecycle state must be in ENDING"
            )


@dataclass(frozen=True, slots=True)
class LifecycleTransitionRequest:
    operation: SessionCommandType
    current_state: LifecycleTransitionState

    def __post_init__(self) -> None:
        if not isinstance(self.operation, SessionCommandType):
            raise LifecycleTransitionContractError(
                "operation must be a SessionCommandType"
            )
        if self.operation not in _SUPPORTED_OPERATIONS:
            raise LifecycleTransitionContractError(
                "operation is not supported by lifecycle transition"
            )
        if not isinstance(self.current_state, LifecycleTransitionState):
            raise LifecycleTransitionContractError(
                "current_state must be a LifecycleTransitionState"
            )


@dataclass(frozen=True, slots=True)
class LifecycleTransitionAccepted:
    operation: SessionCommandType
    previous_state: LifecycleTransitionState
    resulting_state: LifecycleTransitionState
    phase_effect: LifecycleTransitionPhaseEffect

    def __post_init__(self) -> None:
        _require_supported_operation(self.operation)
        if not isinstance(self.previous_state, LifecycleTransitionState):
            raise LifecycleTransitionContractError(
                "previous_state must be a LifecycleTransitionState"
            )
        if not isinstance(self.resulting_state, LifecycleTransitionState):
            raise LifecycleTransitionContractError(
                "resulting_state must be a LifecycleTransitionState"
            )
        if not isinstance(self.phase_effect, LifecycleTransitionPhaseEffect):
            raise LifecycleTransitionContractError(
                "phase_effect must be a LifecycleTransitionPhaseEffect"
            )


@dataclass(frozen=True, slots=True)
class LifecycleTransitionRejected:
    operation: SessionCommandType
    current_state: LifecycleTransitionState
    reason: LifecycleTransitionRejectReason

    def __post_init__(self) -> None:
        _require_supported_operation(self.operation)
        if not isinstance(self.current_state, LifecycleTransitionState):
            raise LifecycleTransitionContractError(
                "current_state must be a LifecycleTransitionState"
            )
        if not isinstance(self.reason, LifecycleTransitionRejectReason):
            raise LifecycleTransitionContractError(
                "reason must be a LifecycleTransitionRejectReason"
            )


LifecycleTransitionDecision = (
    LifecycleTransitionAccepted | LifecycleTransitionRejected
)


def transition_lifecycle(
    request: LifecycleTransitionRequest,
) -> LifecycleTransitionDecision:
    """Return the deterministic domain decision for one lifecycle operation."""

    if not isinstance(request, LifecycleTransitionRequest):
        raise LifecycleTransitionContractError(
            "request must be a LifecycleTransitionRequest"
        )
    current = request.current_state
    if request.operation is SessionCommandType.START_GAME:
        if current.lifecycle_status is not GameSessionStatus.CREATED:
            return _rejected(
                request,
                LifecycleTransitionRejectReason.INVALID_LIFECYCLE_TRANSITION,
            )
        return LifecycleTransitionAccepted(
            operation=request.operation,
            previous_state=current,
            resulting_state=LifecycleTransitionState(
                lifecycle_status=GameSessionStatus.RUNNING,
                phase=GamePhase.INTRODUCTION,
            ),
            phase_effect=LifecycleTransitionPhaseEffect.START_BOOTSTRAP,
        )
    if request.operation is SessionCommandType.PAUSE_GAME:
        if current.lifecycle_status is not GameSessionStatus.RUNNING:
            return _rejected(
                request,
                LifecycleTransitionRejectReason.INVALID_LIFECYCLE_TRANSITION,
            )
        return LifecycleTransitionAccepted(
            operation=request.operation,
            previous_state=current,
            resulting_state=LifecycleTransitionState(
                lifecycle_status=GameSessionStatus.PAUSED,
                phase=current.phase,
            ),
            phase_effect=LifecycleTransitionPhaseEffect.UNCHANGED,
        )
    if current.lifecycle_status is GameSessionStatus.ENDED:
        return _rejected(
            request,
            LifecycleTransitionRejectReason.SESSION_ALREADY_ENDED,
        )
    return LifecycleTransitionAccepted(
        operation=request.operation,
        previous_state=current,
        resulting_state=LifecycleTransitionState(
            lifecycle_status=GameSessionStatus.ENDED,
            phase=GamePhase.ENDING,
        ),
        phase_effect=LifecycleTransitionPhaseEffect.END_TERMINAL,
    )


def _rejected(
    request: LifecycleTransitionRequest,
    reason: LifecycleTransitionRejectReason,
) -> LifecycleTransitionRejected:
    return LifecycleTransitionRejected(
        operation=request.operation,
        current_state=request.current_state,
        reason=reason,
    )


def _require_supported_operation(operation: object) -> None:
    if not isinstance(operation, SessionCommandType):
        raise LifecycleTransitionContractError(
            "operation must be a SessionCommandType"
        )
    if operation not in _SUPPORTED_OPERATIONS:
        raise LifecycleTransitionContractError(
            "operation is not supported by lifecycle transition"
        )
