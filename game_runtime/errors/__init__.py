"""Game Runtime domain errors."""

from game_runtime.errors.exceptions import (
    ActorOwnershipConflict,
    ActionSessionMismatch,
    DuplicateEventError,
    DuplicateActionError,
    EventSessionMismatch,
    GameRuntimeError,
    InvalidActionTransition,
    InvalidPhaseTransition,
    InvalidSessionTransition,
    PersistenceConflict,
    PersistenceError,
    UnsupportedSchemaVersion,
    UnknownActionError,
)

__all__ = [
    "ActorOwnershipConflict",
    "ActionSessionMismatch",
    "DuplicateEventError",
    "DuplicateActionError",
    "EventSessionMismatch",
    "GameRuntimeError",
    "InvalidActionTransition",
    "InvalidPhaseTransition",
    "InvalidSessionTransition",
    "PersistenceConflict",
    "PersistenceError",
    "UnsupportedSchemaVersion",
    "UnknownActionError",
]
