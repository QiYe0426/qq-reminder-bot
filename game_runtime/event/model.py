"""Unified in-memory GameEvent model for P3-A."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Mapping


class GameEventType(str, Enum):
    MESSAGE_RECEIVED = "MESSAGE_RECEIVED"
    DM_COMMAND = "DM_COMMAND"
    PLAYER_STATEMENT = "PLAYER_STATEMENT"
    CLUE_REVEALED = "CLUE_REVEALED"
    PHASE_CHANGED = "PHASE_CHANGED"
    ACTION_REQUESTED = "ACTION_REQUESTED"
    ACTION_COMPLETED = "ACTION_COMPLETED"
    SESSION_RECOVERY = "SESSION_RECOVERY"
    SYSTEM_ERROR = "SYSTEM_ERROR"


class GameEventSource(str, Enum):
    PLATFORM = "PLATFORM"
    CONTROL = "CONTROL"
    DERIVED = "DERIVED"
    ACTION = "ACTION"
    RECOVERY = "RECOVERY"
    SYSTEM = "SYSTEM"


@dataclass(frozen=True, slots=True)
class GameEvent:
    """Structured event envelope that is always bound to one game_id."""

    event_id: str
    game_id: str
    session_id: str
    event_type: GameEventType
    actor: str
    source: GameEventSource
    correlation_id: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.event_id:
            raise ValueError("event_id must not be empty")
        if not self.game_id:
            raise ValueError("game_id must not be empty")
        if not self.session_id:
            raise ValueError("session_id must not be empty")
        if not self.actor:
            raise ValueError("actor must not be empty")
        if not self.correlation_id:
            raise ValueError("correlation_id must not be empty")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
