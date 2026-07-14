"""Typed errors for the P3-A Game Runtime skeleton."""


class GameRuntimeError(Exception):
    """Base error for Game Runtime domain failures."""


class InvalidSessionTransition(GameRuntimeError):
    """Raised when a session lifecycle transition is not allowed."""


class InvalidPhaseTransition(GameRuntimeError):
    """Raised when a game phase transition is not allowed."""


class EventSessionMismatch(GameRuntimeError):
    """Raised when an event is delivered to another session's actor."""


class InvalidActionTransition(GameRuntimeError):
    """Raised when an action status transition is not allowed."""


class ActionSessionMismatch(GameRuntimeError):
    """Raised when an action is delivered to another session's queue."""


class DuplicateActionError(GameRuntimeError):
    """Raised when an action ID is enqueued more than once."""


class UnknownActionError(GameRuntimeError):
    """Raised when an action ID is not present in the queue."""


class PersistenceError(GameRuntimeError):
    """Base error for Game Runtime persistence failures."""


class UnsupportedSchemaVersion(PersistenceError):
    """Raised when the database schema is newer than this Runtime."""


class PersistenceConflict(PersistenceError):
    """Raised when an expected state version or unique binding conflicts."""


class DuplicateEventError(PersistenceError):
    """Raised when an Event ID conflicts with a persisted Event."""


class ActorOwnershipConflict(GameRuntimeError):
    """Raised when two Actors attempt to own the same game_id."""
