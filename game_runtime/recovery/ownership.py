"""Single-process Actor ownership fencing for Game Runtime V0.1."""

from __future__ import annotations

from threading import RLock

from game_runtime.actor import GameSessionActor
from game_runtime.errors import ActorOwnershipConflict


class ActorOwnershipRegistry:
    """Allows at most one in-process Actor for each game_id."""

    def __init__(self) -> None:
        self._actors: dict[str, GameSessionActor] = {}
        self._lock = RLock()

    def acquire_session_owner(self, actor: GameSessionActor) -> None:
        with self._lock:
            if actor.game_id in self._actors:
                raise ActorOwnershipConflict(
                    f"game_id already has an Actor owner: {actor.game_id}"
                )
            self._actors[actor.game_id] = actor

    def get_owner(self, game_id: str) -> GameSessionActor | None:
        with self._lock:
            return self._actors.get(game_id)

    def release_session_owner(self, game_id: str) -> None:
        with self._lock:
            self._actors.pop(game_id, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._actors)
