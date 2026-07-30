"""Pure, deterministic setup script transition contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias


class SetupTransitionContractFailureReason(str, Enum):
    INCOMPLETE_STATE = "INCOMPLETE_STATE"
    EMPTY_REQUESTED_STATE = "EMPTY_REQUESTED_STATE"
    INVALID_STATE_VALUE = "INVALID_STATE_VALUE"


class SetupTransitionContractError(ValueError):
    """The setup transition state or request violates its value contract."""

    __slots__ = ("reason",)

    def __init__(self, reason: SetupTransitionContractFailureReason) -> None:
        if not isinstance(reason, SetupTransitionContractFailureReason):
            raise TypeError(
                "reason must be a SetupTransitionContractFailureReason"
            )
        self.reason = reason
        super().__init__(reason.value)


class SetupTransitionEffect(str, Enum):
    INITIALIZE = "INITIALIZE"
    REPLACE = "REPLACE"


class SetupTransitionRejectReason(str, Enum):
    SCRIPT_ALREADY_SET = "SCRIPT_ALREADY_SET"


@dataclass(frozen=True, slots=True)
class SetupTransitionState:
    script_id: str | None
    manifest_reference: str | None

    def __post_init__(self) -> None:
        if self.script_id is None and self.manifest_reference is None:
            return
        if self.script_id is None or self.manifest_reference is None:
            raise SetupTransitionContractError(
                SetupTransitionContractFailureReason.INCOMPLETE_STATE
            )
        _require_state_value("script_id", self.script_id)
        _require_state_value("manifest_reference", self.manifest_reference)


@dataclass(frozen=True, slots=True)
class SetupTransitionRequest:
    current_state: SetupTransitionState
    requested_state: SetupTransitionState

    def __post_init__(self) -> None:
        _require_state("current_state", self.current_state)
        _require_state("requested_state", self.requested_state)
        if _is_empty(self.requested_state):
            raise SetupTransitionContractError(
                SetupTransitionContractFailureReason.EMPTY_REQUESTED_STATE
            )


@dataclass(frozen=True, slots=True)
class SetupTransitionAccepted:
    previous_state: SetupTransitionState
    resulting_state: SetupTransitionState
    effect: SetupTransitionEffect

    def __post_init__(self) -> None:
        _require_state("previous_state", self.previous_state)
        _require_state("resulting_state", self.resulting_state)
        if not isinstance(self.effect, SetupTransitionEffect):
            raise TypeError("effect must be a SetupTransitionEffect")


@dataclass(frozen=True, slots=True)
class SetupTransitionRejected:
    current_state: SetupTransitionState
    requested_state: SetupTransitionState
    reason: SetupTransitionRejectReason

    def __post_init__(self) -> None:
        _require_state("current_state", self.current_state)
        _require_state("requested_state", self.requested_state)
        if not isinstance(self.reason, SetupTransitionRejectReason):
            raise TypeError("reason must be a SetupTransitionRejectReason")


SetupTransitionDecision: TypeAlias = (
    SetupTransitionAccepted | SetupTransitionRejected
)


def transition_setup(request: SetupTransitionRequest) -> SetupTransitionDecision:
    """Return the value-only decision for one requested setup script change."""

    if not isinstance(request, SetupTransitionRequest):
        raise TypeError("request must be a SetupTransitionRequest")
    if request.current_state == request.requested_state:
        return SetupTransitionRejected(
            current_state=request.current_state,
            requested_state=request.requested_state,
            reason=SetupTransitionRejectReason.SCRIPT_ALREADY_SET,
        )
    return SetupTransitionAccepted(
        previous_state=request.current_state,
        resulting_state=request.requested_state,
        effect=(
            SetupTransitionEffect.INITIALIZE
            if _is_empty(request.current_state)
            else SetupTransitionEffect.REPLACE
        ),
    )


def _is_empty(state: SetupTransitionState) -> bool:
    return state.script_id is None and state.manifest_reference is None


def _require_state(name: str, value: object) -> None:
    if not isinstance(value, SetupTransitionState):
        raise TypeError(f"{name} must be a SetupTransitionState")


def _require_state_value(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise SetupTransitionContractError(
            SetupTransitionContractFailureReason.INVALID_STATE_VALUE
        )
