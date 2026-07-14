"""In-memory adapter from routing to P3-A GameSessionActor."""

from threading import RLock

from game_runtime.actor import GameSessionActor
from game_runtime.event import GameEvent


class ActorGameRuntimeIngress:
    """Test-only actor registry with no platform or persistence integration."""

    def __init__(self) -> None:
        self._actors: dict[tuple[str, str], GameSessionActor] = {}
        self._lock = RLock()

    def register(self, actor: GameSessionActor) -> None:
        key = (actor.game_id, actor.session_id)
        with self._lock:
            self._actors[key] = actor

    def unregister(self, game_id: str, session_id: str) -> None:
        with self._lock:
            self._actors.pop((game_id, session_id), None)

    def accept(self, event: GameEvent) -> bool:
        with self._lock:
            actor = self._actors.get((event.game_id, event.session_id))
        if actor is None:
            return False
        return actor.receive(event)
