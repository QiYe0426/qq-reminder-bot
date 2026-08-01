"""Pure, deterministic Game Rule activation transition contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias


class GameRuleActivationContractFailureReason(str, Enum):
    INCOMPLETE_STATE = "INCOMPLETE_STATE"
    EMPTY_REQUESTED_STATE = "EMPTY_REQUESTED_STATE"
    INVALID_STATE_VALUE = "INVALID_STATE_VALUE"


class GameRuleActivationContractError(ValueError):
    """The activation state or request violates its value contract."""

    __slots__ = ("reason",)

    def __init__(self, reason: GameRuleActivationContractFailureReason) -> None:
        if not isinstance(reason, GameRuleActivationContractFailureReason):
            raise TypeError(
                "reason must be a GameRuleActivationContractFailureReason"
            )
        self.reason = reason
        super().__init__(reason.value)


class GameRuleActivationEffect(str, Enum):
    ACTIVATE_RULE_SET = "ACTIVATE_RULE_SET"


class GameRuleActivationRejectReason(str, Enum):
    RULE_SET_ALREADY_ACTIVE = "RULE_SET_ALREADY_ACTIVE"
    RULE_SET_CONFLICT = "RULE_SET_CONFLICT"


@dataclass(frozen=True, slots=True)
class GameRuleActivationState:
    committed_rule_set_reference: str | None
    opaque_hidden_state_reference: str | None

    def __post_init__(self) -> None:
        if (
            self.committed_rule_set_reference is None
            and self.opaque_hidden_state_reference is None
        ):
            return
        if (
            self.committed_rule_set_reference is None
            or self.opaque_hidden_state_reference is None
        ):
            raise GameRuleActivationContractError(
                GameRuleActivationContractFailureReason.INCOMPLETE_STATE
            )
        _require_state_value(
            "committed_rule_set_reference", self.committed_rule_set_reference
        )
        _require_state_value(
            "opaque_hidden_state_reference", self.opaque_hidden_state_reference
        )


@dataclass(frozen=True, slots=True)
class GameRuleActivationRequest:
    current_state: GameRuleActivationState
    requested_state: GameRuleActivationState

    def __post_init__(self) -> None:
        _require_state("current_state", self.current_state)
        _require_state("requested_state", self.requested_state)
        if _is_empty(self.requested_state):
            raise GameRuleActivationContractError(
                GameRuleActivationContractFailureReason.EMPTY_REQUESTED_STATE
            )


@dataclass(frozen=True, slots=True)
class GameRuleActivationAccepted:
    previous_state: GameRuleActivationState
    resulting_state: GameRuleActivationState
    effect: GameRuleActivationEffect

    def __post_init__(self) -> None:
        _require_state("previous_state", self.previous_state)
        _require_state("resulting_state", self.resulting_state)
        if self.effect is not GameRuleActivationEffect.ACTIVATE_RULE_SET:
            raise TypeError("effect must be GameRuleActivationEffect.ACTIVATE_RULE_SET")


@dataclass(frozen=True, slots=True)
class GameRuleActivationRejected:
    current_state: GameRuleActivationState
    requested_state: GameRuleActivationState
    reason: GameRuleActivationRejectReason

    def __post_init__(self) -> None:
        _require_state("current_state", self.current_state)
        _require_state("requested_state", self.requested_state)
        if not isinstance(self.reason, GameRuleActivationRejectReason):
            raise TypeError("reason must be a GameRuleActivationRejectReason")


GameRuleActivationDecision: TypeAlias = (
    GameRuleActivationAccepted | GameRuleActivationRejected
)


def transition_game_rule_activation(
    request: GameRuleActivationRequest,
) -> GameRuleActivationDecision:
    """Return the value-only activation decision for one requested rule set."""

    if not isinstance(request, GameRuleActivationRequest):
        raise TypeError("request must be a GameRuleActivationRequest")
    if _is_empty(request.current_state):
        return GameRuleActivationAccepted(
            previous_state=request.current_state,
            resulting_state=request.requested_state,
            effect=GameRuleActivationEffect.ACTIVATE_RULE_SET,
        )
    if request.current_state == request.requested_state:
        return GameRuleActivationRejected(
            current_state=request.current_state,
            requested_state=request.requested_state,
            reason=GameRuleActivationRejectReason.RULE_SET_ALREADY_ACTIVE,
        )
    return GameRuleActivationRejected(
        current_state=request.current_state,
        requested_state=request.requested_state,
        reason=GameRuleActivationRejectReason.RULE_SET_CONFLICT,
    )


def _is_empty(state: GameRuleActivationState) -> bool:
    return (
        state.committed_rule_set_reference is None
        and state.opaque_hidden_state_reference is None
    )


def _require_state(name: str, value: object) -> None:
    if not isinstance(value, GameRuleActivationState):
        raise TypeError(f"{name} must be a GameRuleActivationState")


def _require_state_value(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise GameRuleActivationContractError(
            GameRuleActivationContractFailureReason.INVALID_STATE_VALUE
        )
