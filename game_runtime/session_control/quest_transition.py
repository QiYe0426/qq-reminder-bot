"""Pure, deterministic activation transition for the Quest Domain."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias

from game_runtime.session_control.quest_evidence import (
    QuestActivationDisposition,
)


class QuestActivationContractFailureReason(str, Enum):
    INVALID_STATE = "INVALID_STATE"
    INVALID_REQUEST = "INVALID_REQUEST"
    UNKNOWN_DISPOSITION = "UNKNOWN_DISPOSITION"
    INVALID_REQUESTED_STATE = "INVALID_REQUESTED_STATE"


class QuestActivationContractError(ValueError):
    __slots__ = ("reason",)

    def __init__(self, reason: QuestActivationContractFailureReason) -> None:
        if not isinstance(reason, QuestActivationContractFailureReason):
            raise TypeError(
                "reason must be a QuestActivationContractFailureReason"
            )
        self.reason = reason
        super().__init__(reason.value)


class QuestActivationEffect(str, Enum):
    ACTIVATE = "ACTIVATE"


class QuestActivationRejectReason(str, Enum):
    QUEST_ALREADY_ACTIVE = "QUEST_ALREADY_ACTIVE"
    QUEST_CONFLICT = "QUEST_CONFLICT"
    QUEST_NOT_FOUND = "QUEST_NOT_FOUND"
    QUEST_NOT_ACTIVATABLE = "QUEST_NOT_ACTIVATABLE"
    RULE_SET_NOT_ACTIVE = "RULE_SET_NOT_ACTIVE"


@dataclass(frozen=True, slots=True)
class QuestActivationState:
    active_quest_id: str | None
    source_rule_set_reference: str | None
    committed_public_state_reference: str | None

    def __post_init__(self) -> None:
        values = (
            self.active_quest_id,
            self.source_rule_set_reference,
            self.committed_public_state_reference,
        )
        present = tuple(value is not None for value in values)
        if any(present) and not all(present):
            raise QuestActivationContractError(
                QuestActivationContractFailureReason.INVALID_STATE
            )
        for value in values:
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise QuestActivationContractError(
                    QuestActivationContractFailureReason.INVALID_STATE
                )


@dataclass(frozen=True, slots=True)
class QuestActivationRequest:
    quest_id: str
    current_state: QuestActivationState
    requested_state: QuestActivationState | None
    disposition: QuestActivationDisposition

    def __post_init__(self) -> None:
        if not isinstance(self.quest_id, str) or not self.quest_id.strip():
            raise QuestActivationContractError(
                QuestActivationContractFailureReason.INVALID_REQUEST
            )
        if not isinstance(self.current_state, QuestActivationState):
            raise TypeError("current_state must be a QuestActivationState")
        if self.requested_state is not None and not isinstance(
            self.requested_state,
            QuestActivationState,
        ):
            raise TypeError(
                "requested_state must be a QuestActivationState or None"
            )
        if not isinstance(self.disposition, QuestActivationDisposition):
            raise TypeError("disposition must be a QuestActivationDisposition")
        if self.disposition is QuestActivationDisposition.UNKNOWN:
            raise QuestActivationContractError(
                QuestActivationContractFailureReason.UNKNOWN_DISPOSITION
            )
        if self.disposition is QuestActivationDisposition.AVAILABLE:
            self._validate_available()
            return
        if self.requested_state is not None:
            raise QuestActivationContractError(
                QuestActivationContractFailureReason.INVALID_REQUESTED_STATE
            )
        current_id = self.current_state.active_quest_id
        if (
            self.disposition is QuestActivationDisposition.ALREADY_ACTIVE
            and current_id != self.quest_id
        ):
            raise QuestActivationContractError(
                QuestActivationContractFailureReason.INVALID_REQUEST
            )
        if (
            self.disposition is QuestActivationDisposition.CONFLICT
            and (current_id is None or current_id == self.quest_id)
        ):
            raise QuestActivationContractError(
                QuestActivationContractFailureReason.INVALID_REQUEST
            )
        if self.disposition in {
            QuestActivationDisposition.NOT_FOUND,
            QuestActivationDisposition.NOT_ACTIVATABLE,
            QuestActivationDisposition.RULE_SET_NOT_ACTIVE,
        } and current_id is not None:
            raise QuestActivationContractError(
                QuestActivationContractFailureReason.INVALID_REQUEST
            )

    def _validate_available(self) -> None:
        requested = self.requested_state
        if (
            self.current_state.active_quest_id is not None
            or requested is None
            or requested.active_quest_id != self.quest_id
        ):
            raise QuestActivationContractError(
                QuestActivationContractFailureReason.INVALID_REQUESTED_STATE
            )


@dataclass(frozen=True, slots=True)
class QuestActivationAccepted:
    quest_id: str
    previous_state: QuestActivationState
    resulting_state: QuestActivationState
    effect: QuestActivationEffect

    def __post_init__(self) -> None:
        if not isinstance(self.quest_id, str) or not self.quest_id.strip():
            raise QuestActivationContractError(
                QuestActivationContractFailureReason.INVALID_REQUEST
            )
        if not isinstance(self.previous_state, QuestActivationState):
            raise TypeError("previous_state must be a QuestActivationState")
        if not isinstance(self.resulting_state, QuestActivationState):
            raise TypeError("resulting_state must be a QuestActivationState")
        if not isinstance(self.effect, QuestActivationEffect):
            raise TypeError("effect must be a QuestActivationEffect")
        if (
            self.effect is not QuestActivationEffect.ACTIVATE
            or self.previous_state.active_quest_id is not None
            or self.resulting_state.active_quest_id != self.quest_id
        ):
            raise QuestActivationContractError(
                QuestActivationContractFailureReason.INVALID_REQUESTED_STATE
            )


@dataclass(frozen=True, slots=True)
class QuestActivationRejected:
    quest_id: str
    current_state: QuestActivationState
    reason: QuestActivationRejectReason

    def __post_init__(self) -> None:
        if not isinstance(self.quest_id, str) or not self.quest_id.strip():
            raise QuestActivationContractError(
                QuestActivationContractFailureReason.INVALID_REQUEST
            )
        if not isinstance(self.current_state, QuestActivationState):
            raise TypeError("current_state must be a QuestActivationState")
        if not isinstance(self.reason, QuestActivationRejectReason):
            raise TypeError("reason must be a QuestActivationRejectReason")


QuestActivationDecision: TypeAlias = (
    QuestActivationAccepted | QuestActivationRejected
)


def transition_quest_activation(
    request: QuestActivationRequest,
) -> QuestActivationDecision:
    if not isinstance(request, QuestActivationRequest):
        raise TypeError("request must be a QuestActivationRequest")
    if request.disposition is QuestActivationDisposition.AVAILABLE:
        requested = request.requested_state
        if requested is None:
            raise QuestActivationContractError(
                QuestActivationContractFailureReason.INVALID_REQUESTED_STATE
            )
        return QuestActivationAccepted(
            quest_id=request.quest_id,
            previous_state=request.current_state,
            resulting_state=requested,
            effect=QuestActivationEffect.ACTIVATE,
        )
    reason = {
        QuestActivationDisposition.ALREADY_ACTIVE:
            QuestActivationRejectReason.QUEST_ALREADY_ACTIVE,
        QuestActivationDisposition.CONFLICT:
            QuestActivationRejectReason.QUEST_CONFLICT,
        QuestActivationDisposition.NOT_FOUND:
            QuestActivationRejectReason.QUEST_NOT_FOUND,
        QuestActivationDisposition.NOT_ACTIVATABLE:
            QuestActivationRejectReason.QUEST_NOT_ACTIVATABLE,
        QuestActivationDisposition.RULE_SET_NOT_ACTIVE:
            QuestActivationRejectReason.RULE_SET_NOT_ACTIVE,
    }.get(request.disposition)
    if reason is None:
        raise QuestActivationContractError(
            QuestActivationContractFailureReason.UNKNOWN_DISPOSITION
        )
    return QuestActivationRejected(
        quest_id=request.quest_id,
        current_state=request.current_state,
        reason=reason,
    )
