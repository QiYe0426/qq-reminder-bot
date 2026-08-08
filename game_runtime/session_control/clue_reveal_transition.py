"""Pure, deterministic transition for public clue disclosure."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias

from game_runtime.session_control.game_rule_evidence import ClueRevealDisposition


class ClueRevealContractFailureReason(str, Enum):
    INVALID_STATE = "INVALID_STATE"
    INVALID_REQUEST = "INVALID_REQUEST"
    UNKNOWN_DISPOSITION = "UNKNOWN_DISPOSITION"
    INVALID_CANDIDATE = "INVALID_CANDIDATE"


class ClueRevealContractError(ValueError):
    __slots__ = ("reason",)

    def __init__(self, reason: ClueRevealContractFailureReason) -> None:
        if not isinstance(reason, ClueRevealContractFailureReason):
            raise TypeError("reason must be a ClueRevealContractFailureReason")
        self.reason = reason
        super().__init__(reason.value)


class ClueRevealEffect(str, Enum):
    PUBLIC_REVEAL = "PUBLIC_REVEAL"


class ClueRevealRejectReason(str, Enum):
    RULE_SET_NOT_ACTIVE = "RULE_SET_NOT_ACTIVE"
    CLUE_ALREADY_REVEALED = "CLUE_ALREADY_REVEALED"
    CLUE_NOT_FOUND = "CLUE_NOT_FOUND"
    CLUE_NOT_REVEALABLE = "CLUE_NOT_REVEALABLE"


@dataclass(frozen=True, slots=True)
class ClueRevealState:
    rule_set_reference: str | None
    disclosure_state_reference: str | None
    hidden_state_reference: str | None

    def __post_init__(self) -> None:
        values = (
            self.rule_set_reference,
            self.disclosure_state_reference,
            self.hidden_state_reference,
        )
        present = tuple(value is not None for value in values)
        if any(present) and not all(present):
            raise ClueRevealContractError(
                ClueRevealContractFailureReason.INVALID_STATE
            )
        for value in values:
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ClueRevealContractError(
                    ClueRevealContractFailureReason.INVALID_STATE
                )


@dataclass(frozen=True, slots=True)
class ClueRevealRequest:
    clue_id: str
    current_state: ClueRevealState
    candidate_state: ClueRevealState | None
    disposition: ClueRevealDisposition

    def __post_init__(self) -> None:
        if not isinstance(self.clue_id, str) or not self.clue_id.strip():
            raise ClueRevealContractError(
                ClueRevealContractFailureReason.INVALID_REQUEST
            )
        if not isinstance(self.current_state, ClueRevealState):
            raise TypeError("current_state must be a ClueRevealState")
        if self.candidate_state is not None and not isinstance(
            self.candidate_state, ClueRevealState
        ):
            raise TypeError("candidate_state must be a ClueRevealState or None")
        if not isinstance(self.disposition, ClueRevealDisposition):
            raise TypeError("disposition must be a ClueRevealDisposition")
        if self.disposition is ClueRevealDisposition.UNKNOWN:
            raise ClueRevealContractError(
                ClueRevealContractFailureReason.UNKNOWN_DISPOSITION
            )
        if self.disposition is ClueRevealDisposition.AVAILABLE:
            self._validate_available_candidate()
            return
        if self.candidate_state is not None:
            raise ClueRevealContractError(
                ClueRevealContractFailureReason.INVALID_CANDIDATE
            )
        current_is_empty = self.current_state.rule_set_reference is None
        if (
            self.disposition is ClueRevealDisposition.RULE_SET_NOT_ACTIVE
        ) != current_is_empty:
            raise ClueRevealContractError(
                ClueRevealContractFailureReason.INVALID_REQUEST
            )

    def _validate_available_candidate(self) -> None:
        candidate = self.candidate_state
        if candidate is None or self.current_state.rule_set_reference is None:
            raise ClueRevealContractError(
                ClueRevealContractFailureReason.INVALID_CANDIDATE
            )
        if (
            candidate.rule_set_reference != self.current_state.rule_set_reference
            or candidate.disclosure_state_reference
            == self.current_state.disclosure_state_reference
            or candidate.hidden_state_reference
            == self.current_state.hidden_state_reference
        ):
            raise ClueRevealContractError(
                ClueRevealContractFailureReason.INVALID_CANDIDATE
            )


@dataclass(frozen=True, slots=True)
class ClueRevealAccepted:
    clue_id: str
    previous_state: ClueRevealState
    resulting_state: ClueRevealState
    effect: ClueRevealEffect

    def __post_init__(self) -> None:
        if not isinstance(self.clue_id, str) or not self.clue_id.strip():
            raise ClueRevealContractError(
                ClueRevealContractFailureReason.INVALID_REQUEST
            )
        if not isinstance(self.previous_state, ClueRevealState):
            raise TypeError("previous_state must be a ClueRevealState")
        if not isinstance(self.resulting_state, ClueRevealState):
            raise TypeError("resulting_state must be a ClueRevealState")
        if self.effect is not ClueRevealEffect.PUBLIC_REVEAL:
            raise TypeError("effect must be ClueRevealEffect.PUBLIC_REVEAL")


@dataclass(frozen=True, slots=True)
class ClueRevealRejected:
    clue_id: str
    current_state: ClueRevealState
    reason: ClueRevealRejectReason

    def __post_init__(self) -> None:
        if not isinstance(self.clue_id, str) or not self.clue_id.strip():
            raise ClueRevealContractError(
                ClueRevealContractFailureReason.INVALID_REQUEST
            )
        if not isinstance(self.current_state, ClueRevealState):
            raise TypeError("current_state must be a ClueRevealState")
        if not isinstance(self.reason, ClueRevealRejectReason):
            raise TypeError("reason must be a ClueRevealRejectReason")


ClueRevealDecision: TypeAlias = ClueRevealAccepted | ClueRevealRejected


def transition_clue_reveal(request: ClueRevealRequest) -> ClueRevealDecision:
    if not isinstance(request, ClueRevealRequest):
        raise TypeError("request must be a ClueRevealRequest")
    if request.disposition is ClueRevealDisposition.AVAILABLE:
        candidate = request.candidate_state
        if candidate is None:
            raise ClueRevealContractError(
                ClueRevealContractFailureReason.INVALID_CANDIDATE
            )
        return ClueRevealAccepted(
            clue_id=request.clue_id,
            previous_state=request.current_state,
            resulting_state=candidate,
            effect=ClueRevealEffect.PUBLIC_REVEAL,
        )
    reason = {
        ClueRevealDisposition.RULE_SET_NOT_ACTIVE:
            ClueRevealRejectReason.RULE_SET_NOT_ACTIVE,
        ClueRevealDisposition.ALREADY_REVEALED:
            ClueRevealRejectReason.CLUE_ALREADY_REVEALED,
        ClueRevealDisposition.NOT_FOUND:
            ClueRevealRejectReason.CLUE_NOT_FOUND,
        ClueRevealDisposition.NOT_REVEALABLE:
            ClueRevealRejectReason.CLUE_NOT_REVEALABLE,
    }.get(request.disposition)
    if reason is None:
        raise ClueRevealContractError(
            ClueRevealContractFailureReason.UNKNOWN_DISPOSITION
        )
    return ClueRevealRejected(
        clue_id=request.clue_id,
        current_state=request.current_state,
        reason=reason,
    )
