"""Isolated persistence infrastructure for Game Runtime."""

from game_runtime.persistence.database import (
    DEFAULT_GAME_DATABASE_PATH,
    SQLiteGameDatabase,
)
from game_runtime.persistence.records import (
    EventProcessingStatus,
    PersistentOwnership,
    StoredGameEvent,
)
from game_runtime.persistence.repositories import (
    GameActionRepository,
    GameEventRepository,
    GameSessionRepository,
    ParticipantRepository,
    RuntimeStateRepository,
)
from game_runtime.persistence.schema import CURRENT_SCHEMA_VERSION

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "DEFAULT_GAME_DATABASE_PATH",
    "EventProcessingStatus",
    "GameActionRepository",
    "GameEventRepository",
    "GameSessionRepository",
    "ParticipantRepository",
    "PersistentOwnership",
    "SQLiteGameDatabase",
    "StoredGameEvent",
    "RuntimeStateRepository",
]
