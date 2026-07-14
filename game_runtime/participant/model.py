"""Minimal participant contracts for P3-A."""

from dataclasses import dataclass
from enum import Enum


class ParticipantType(str, Enum):
    DM = "DM"
    PLAYER = "PLAYER"
    SPECTATOR = "SPECTATOR"
    UNKNOWN = "UNKNOWN"


class ParticipantMembershipState(str, Enum):
    ACTIVE = "ACTIVE"
    REPLACED = "REPLACED"
    LEFT = "LEFT"
    REVOKED = "REVOKED"


@dataclass(frozen=True, slots=True)
class ParticipantReference:
    """Session-local participant reference without profile or memory data."""

    participant_id: str
    qq_id: str
    participant_type: ParticipantType
    character_id: str | None = None
    membership_state: ParticipantMembershipState = ParticipantMembershipState.ACTIVE

    def __post_init__(self) -> None:
        if not self.participant_id:
            raise ValueError("participant_id must not be empty")
        if not self.qq_id:
            raise ValueError("qq_id must not be empty")
