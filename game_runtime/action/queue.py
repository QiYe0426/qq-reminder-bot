"""In-memory Action Queue contract implementation for P3-A tests."""

from __future__ import annotations

from collections import deque
from threading import RLock

from game_runtime.action.model import GameAction, GameActionStatus
from game_runtime.errors import (
    ActionSessionMismatch,
    DuplicateActionError,
    UnknownActionError,
)


class ActionQueue:
    """Session-scoped queue that only applies Action state transitions."""

    def __init__(self, game_id: str, session_id: str) -> None:
        if not game_id:
            raise ValueError("game_id must not be empty")
        if not session_id:
            raise ValueError("session_id must not be empty")
        self._game_id = game_id
        self._session_id = session_id
        self._actions: dict[str, GameAction] = {}
        self._pending_ids: deque[str] = deque()
        self._lock = RLock()

    @property
    def game_id(self) -> str:
        return self._game_id

    @property
    def session_id(self) -> str:
        return self._session_id

    def enqueue(self, action: GameAction) -> None:
        with self._lock:
            if (
                action.game_id != self._game_id
                or action.session_id != self._session_id
            ):
                raise ActionSessionMismatch(
                    f"action scope ({action.game_id!r}, {action.session_id!r}) does "
                    f"not match queue scope ({self._game_id!r}, {self._session_id!r})"
                )
            if action.action_id in self._actions:
                raise DuplicateActionError(f"duplicate action_id: {action.action_id}")
            if action.status is not GameActionStatus.CREATED:
                raise ValueError("only CREATED actions can be enqueued")
            self._actions[action.action_id] = action
            self._pending_ids.append(action.action_id)

    def claim_next(self) -> GameAction | None:
        with self._lock:
            while self._pending_ids:
                action = self._actions[self._pending_ids.popleft()]
                if action.status is GameActionStatus.CREATED:
                    action.transition_to(GameActionStatus.EXECUTING)
                    return action
            return None

    def complete(self, action_id: str, status: GameActionStatus) -> GameAction:
        if status not in {
            GameActionStatus.SUCCESS,
            GameActionStatus.FAILED,
            GameActionStatus.UNKNOWN,
        }:
            raise ValueError("completion status must be SUCCESS, FAILED, or UNKNOWN")
        with self._lock:
            action = self.get(action_id)
            action.transition_to(status)
            return action

    def get(self, action_id: str) -> GameAction:
        try:
            return self._actions[action_id]
        except KeyError as exc:
            raise UnknownActionError(f"unknown action_id: {action_id}") from exc

    def __len__(self) -> int:
        return len(self._actions)
