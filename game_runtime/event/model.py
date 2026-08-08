"""Unified in-memory GameEvent envelope."""

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
    SESSION_CREATED = "SESSION_CREATED"
    SESSION_STARTED = "SESSION_STARTED"
    SESSION_PAUSED = "SESSION_PAUSED"
    SESSION_RESUMED = "SESSION_RESUMED"
    SESSION_ENDED = "SESSION_ENDED"
    SCRIPT_SET = "SCRIPT_SET"
    CHARACTER_ASSIGNED = "CHARACTER_ASSIGNED"
    PLAYER_REPLACED = "PLAYER_REPLACED"
    RULE_SET_ACTIVATED = "RULE_SET_ACTIVATED"
    QUEST_ACTIVATED = "QUEST_ACTIVATED"
    SESSION_CONTROL_REJECTED = "SESSION_CONTROL_REJECTED"


class GameEventSource(str, Enum):
    PLATFORM = "PLATFORM"
    CONTROL = "CONTROL"
    DERIVED = "DERIVED"
    ACTION = "ACTION"
    RECOVERY = "RECOVERY"
    SYSTEM = "SYSTEM"


class EventVisibility(str, Enum):
    PUBLIC = "PUBLIC"
    CHARACTER_PRIVATE = "CHARACTER_PRIVATE"
    DM_CONTROL = "DM_CONTROL"
    SYSTEM_ONLY = "SYSTEM_ONLY"


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
    schema_version: int = 1
    visibility: EventVisibility = EventVisibility.SYSTEM_ONLY
    observed_state_version: int | None = None
    causation_event_id: str | None = None

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
        if not isinstance(self.event_type, GameEventType):
            raise TypeError("event_type must be a GameEventType")
        if not isinstance(self.source, GameEventSource):
            raise TypeError("source must be a GameEventSource")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a Mapping")
        if not isinstance(self.schema_version, int) or isinstance(
            self.schema_version, bool
        ):
            raise TypeError("schema_version must be an integer")
        if self.schema_version < 1:
            raise ValueError("schema_version must be positive")
        if not isinstance(self.visibility, EventVisibility):
            raise TypeError("visibility must be an EventVisibility")
        if self.observed_state_version is not None:
            if not isinstance(self.observed_state_version, int) or isinstance(
                self.observed_state_version, bool
            ):
                raise TypeError("observed_state_version must be an integer or None")
            if self.observed_state_version < 0:
                raise ValueError("observed_state_version must not be negative")
        if self.causation_event_id is not None:
            if not isinstance(self.causation_event_id, str):
                raise TypeError("causation_event_id must be a string or None")
            if not self.causation_event_id.strip():
                raise ValueError("causation_event_id must not be empty")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")

        # Result Events are closed schemas. The local import keeps the shared
        # envelope independent at module load time while preventing producers
        # from constructing an invalid control result Event.
        from game_runtime.event.control_payloads import (
            CONTROL_RESULT_PAYLOAD_TYPES,
            validate_control_result_event,
        )

        if self.event_type in CONTROL_RESULT_PAYLOAD_TYPES:
            validate_control_result_event(self)
