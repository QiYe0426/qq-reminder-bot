"""Persistence records that keep storage metadata out of domain envelopes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from game_runtime.event import GameEvent
from game_runtime.routing.models import SessionLookupStatus


class EventProcessingStatus(str, Enum):
    RECEIVED = "RECEIVED"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    DEFERRED = "DEFERRED"


@dataclass(frozen=True, slots=True)
class StoredGameEvent:
    event: GameEvent
    sequence_no: int
    processing_status: EventProcessingStatus
    applied_state_version: int | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class PersistentOwnership:
    group_id: str
    game_id: str
    session_id: str
    session_status: SessionLookupStatus
    ownership_generation: int
    state_version: int
    updated_at: datetime
