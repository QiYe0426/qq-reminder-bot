"""Pure, deterministic participant record transition contract."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import TypeAlias

from game_runtime.participant import ParticipantMembershipState, ParticipantType


class ParticipantTransitionContractFailureReason(str, Enum):
    INVALID_RECORD_VALUE = "INVALID_RECORD_VALUE"
    INVALID_REQUEST_VALUE = "INVALID_REQUEST_VALUE"
    DUPLICATE_PARTICIPANT_ID = "DUPLICATE_PARTICIPANT_ID"


class ParticipantTransitionContractError(ValueError):
    """The participant record or transition request violates its value contract."""

    __slots__ = ("reason",)

    def __init__(self, reason: ParticipantTransitionContractFailureReason) -> None:
        if not isinstance(reason, ParticipantTransitionContractFailureReason):
            raise TypeError(
                "reason must be a ParticipantTransitionContractFailureReason"
            )
        self.reason = reason
        super().__init__(reason.value)


class ParticipantTransitionEffect(str, Enum):
    ASSIGN_CHARACTER = "ASSIGN_CHARACTER"
    REPLACE_PLAYER = "REPLACE_PLAYER"


class ParticipantTransitionRejectReason(str, Enum):
    INVALID_CHARACTER_ASSIGNMENT = "INVALID_CHARACTER_ASSIGNMENT"
    CHARACTER_ALREADY_ASSIGNED = "CHARACTER_ALREADY_ASSIGNED"
    CHARACTER_CONFLICT = "CHARACTER_CONFLICT"
    INVALID_REPLACEMENT = "INVALID_REPLACEMENT"


@dataclass(frozen=True, slots=True)
class ParticipantTransitionRecordState:
    participant_id: str
    participant_type: ParticipantType
    membership_state: ParticipantMembershipState
    character_id: str | None
    binding_version: int

    def __post_init__(self) -> None:
        _require_string("participant_id", self.participant_id, record=True)
        if not isinstance(self.participant_type, ParticipantType):
            raise TypeError("participant_type must be a ParticipantType")
        if not isinstance(self.membership_state, ParticipantMembershipState):
            raise TypeError("membership_state must be a ParticipantMembershipState")
        if self.character_id is not None:
            _require_string("character_id", self.character_id, record=True)
        if isinstance(self.binding_version, bool) or not isinstance(
            self.binding_version, int
        ):
            raise TypeError("binding_version must be an integer")
        if self.binding_version < 0:
            raise ParticipantTransitionContractError(
                ParticipantTransitionContractFailureReason.INVALID_RECORD_VALUE
            )


@dataclass(frozen=True, slots=True)
class AssignCharacterTransitionRequest:
    current_participant: ParticipantTransitionRecordState
    requested_character_id: str

    def __post_init__(self) -> None:
        _require_record("current_participant", self.current_participant)
        _require_string("requested_character_id", self.requested_character_id, record=False)


@dataclass(frozen=True, slots=True)
class ReplacePlayerTransitionRequest:
    old_participant: ParticipantTransitionRecordState
    new_participant: ParticipantTransitionRecordState

    def __post_init__(self) -> None:
        _require_record("old_participant", self.old_participant)
        _require_record("new_participant", self.new_participant)
        if self.old_participant.participant_id == self.new_participant.participant_id:
            raise ParticipantTransitionContractError(
                ParticipantTransitionContractFailureReason.DUPLICATE_PARTICIPANT_ID
            )


ParticipantTransitionRequest: TypeAlias = (
    AssignCharacterTransitionRequest | ReplacePlayerTransitionRequest
)


@dataclass(frozen=True, slots=True)
class AssignCharacterTransitionAccepted:
    previous_participant: ParticipantTransitionRecordState
    resulting_participant: ParticipantTransitionRecordState
    effect: ParticipantTransitionEffect

    def __post_init__(self) -> None:
        _require_record("previous_participant", self.previous_participant)
        _require_record("resulting_participant", self.resulting_participant)
        if self.effect is not ParticipantTransitionEffect.ASSIGN_CHARACTER:
            raise TypeError("effect must be ParticipantTransitionEffect.ASSIGN_CHARACTER")


@dataclass(frozen=True, slots=True)
class ReplacePlayerTransitionAccepted:
    previous_old_participant: ParticipantTransitionRecordState
    previous_new_participant: ParticipantTransitionRecordState
    resulting_old_participant: ParticipantTransitionRecordState
    resulting_new_participant: ParticipantTransitionRecordState
    effect: ParticipantTransitionEffect

    def __post_init__(self) -> None:
        for name, value in (
            ("previous_old_participant", self.previous_old_participant),
            ("previous_new_participant", self.previous_new_participant),
            ("resulting_old_participant", self.resulting_old_participant),
            ("resulting_new_participant", self.resulting_new_participant),
        ):
            _require_record(name, value)
        if self.effect is not ParticipantTransitionEffect.REPLACE_PLAYER:
            raise TypeError("effect must be ParticipantTransitionEffect.REPLACE_PLAYER")


@dataclass(frozen=True, slots=True)
class AssignCharacterTransitionRejected:
    current_participant: ParticipantTransitionRecordState
    requested_character_id: str
    reason: ParticipantTransitionRejectReason

    def __post_init__(self) -> None:
        _require_record("current_participant", self.current_participant)
        _require_string("requested_character_id", self.requested_character_id, record=False)
        _require_reject_reason(self.reason)


@dataclass(frozen=True, slots=True)
class ReplacePlayerTransitionRejected:
    old_participant: ParticipantTransitionRecordState
    new_participant: ParticipantTransitionRecordState
    reason: ParticipantTransitionRejectReason

    def __post_init__(self) -> None:
        _require_record("old_participant", self.old_participant)
        _require_record("new_participant", self.new_participant)
        _require_reject_reason(self.reason)


ParticipantTransitionDecision: TypeAlias = (
    AssignCharacterTransitionAccepted
    | AssignCharacterTransitionRejected
    | ReplacePlayerTransitionAccepted
    | ReplacePlayerTransitionRejected
)


def transition_participant(
    request: ParticipantTransitionRequest,
) -> ParticipantTransitionDecision:
    """Return the value-only decision for one participant record transition."""

    if isinstance(request, AssignCharacterTransitionRequest):
        return _assign_character(request)
    if isinstance(request, ReplacePlayerTransitionRequest):
        return _replace_player(request)
    raise TypeError("request must be a ParticipantTransitionRequest")


def _assign_character(
    request: AssignCharacterTransitionRequest,
) -> AssignCharacterTransitionAccepted | AssignCharacterTransitionRejected:
    participant = request.current_participant
    if (
        participant.participant_type is not ParticipantType.PLAYER
        or participant.membership_state is not ParticipantMembershipState.ACTIVE
    ):
        return AssignCharacterTransitionRejected(
            participant,
            request.requested_character_id,
            ParticipantTransitionRejectReason.INVALID_CHARACTER_ASSIGNMENT,
        )
    if participant.character_id == request.requested_character_id:
        return AssignCharacterTransitionRejected(
            participant,
            request.requested_character_id,
            ParticipantTransitionRejectReason.CHARACTER_ALREADY_ASSIGNED,
        )
    if participant.character_id is not None:
        return AssignCharacterTransitionRejected(
            participant,
            request.requested_character_id,
            ParticipantTransitionRejectReason.CHARACTER_CONFLICT,
        )
    return AssignCharacterTransitionAccepted(
        participant,
        replace(
            participant,
            character_id=request.requested_character_id,
            binding_version=participant.binding_version + 1,
        ),
        ParticipantTransitionEffect.ASSIGN_CHARACTER,
    )


def _replace_player(
    request: ReplacePlayerTransitionRequest,
) -> ReplacePlayerTransitionAccepted | ReplacePlayerTransitionRejected:
    old, new = request.old_participant, request.new_participant
    if not (
        old.participant_type is ParticipantType.PLAYER
        and new.participant_type is ParticipantType.PLAYER
        and old.membership_state is ParticipantMembershipState.ACTIVE
        and new.membership_state is ParticipantMembershipState.ACTIVE
        and new.character_id is None
    ):
        return ReplacePlayerTransitionRejected(
            old, new, ParticipantTransitionRejectReason.INVALID_REPLACEMENT
        )
    return ReplacePlayerTransitionAccepted(
        old,
        new,
        replace(
            old,
            membership_state=ParticipantMembershipState.REPLACED,
            character_id=None,
            binding_version=old.binding_version + 1,
        ),
        replace(
            new,
            character_id=old.character_id,
            binding_version=new.binding_version + 1,
        ),
        ParticipantTransitionEffect.REPLACE_PLAYER,
    )


def _require_record(name: str, value: object) -> None:
    if not isinstance(value, ParticipantTransitionRecordState):
        raise TypeError(f"{name} must be a ParticipantTransitionRecordState")


def _require_string(name: str, value: object, *, record: bool) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ParticipantTransitionContractError(
            (
                ParticipantTransitionContractFailureReason.INVALID_RECORD_VALUE
                if record
                else ParticipantTransitionContractFailureReason.INVALID_REQUEST_VALUE
            )
        )


def _require_reject_reason(value: object) -> None:
    if not isinstance(value, ParticipantTransitionRejectReason):
        raise TypeError("reason must be a ParticipantTransitionRejectReason")
