"""Game Action state machine without an executor."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from game_runtime.errors import InvalidActionTransition


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class GameActionStatus(str, Enum):
    CREATED = "CREATED"
    EXECUTING = "EXECUTING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"


_ACTION_TRANSITIONS: dict[GameActionStatus, frozenset[GameActionStatus]] = {
    GameActionStatus.CREATED: frozenset(
        {GameActionStatus.EXECUTING, GameActionStatus.CANCELLED}
    ),
    GameActionStatus.EXECUTING: frozenset(
        {
            GameActionStatus.SUCCESS,
            GameActionStatus.FAILED,
            GameActionStatus.UNKNOWN,
        }
    ),
    GameActionStatus.SUCCESS: frozenset(),
    GameActionStatus.FAILED: frozenset(),
    GameActionStatus.UNKNOWN: frozenset(),
    GameActionStatus.CANCELLED: frozenset(),
}


@dataclass(slots=True)
class GameAction:
    """In-memory Action record with no communication side effects."""

    action_id: str
    game_id: str
    session_id: str
    action_type: str
    status: GameActionStatus = GameActionStatus.CREATED
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.action_id:
            raise ValueError("action_id must not be empty")
        if not self.game_id:
            raise ValueError("game_id must not be empty")
        if not self.session_id:
            raise ValueError("session_id must not be empty")
        if not self.action_type:
            raise ValueError("action_type must not be empty")
        for name, value in (("created_at", self.created_at), ("updated_at", self.updated_at)):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")

    def can_transition_to(self, target: GameActionStatus) -> bool:
        return target in _ACTION_TRANSITIONS[self.status]

    def transition_to(
        self,
        target: GameActionStatus,
        *,
        occurred_at: datetime | None = None,
    ) -> None:
        if not self.can_transition_to(target):
            raise InvalidActionTransition(
                f"action transition {self.status.value} -> {target.value} is not allowed"
            )
        next_updated_at = occurred_at or utc_now()
        if next_updated_at.tzinfo is None or next_updated_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        if next_updated_at < self.updated_at:
            raise ValueError("occurred_at must not move updated_at backwards")
        self.status = target
        self.updated_at = next_updated_at
