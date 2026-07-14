"""In-memory Session Registry for P3-B contract verification only."""

from threading import RLock

from game_runtime.routing.models import SessionLookupStatus, SessionOwnershipSnapshot
from game_runtime.session import GameSession, GameSessionStatus


_SESSION_STATUS_MAP = {
    GameSessionStatus.RUNNING: SessionLookupStatus.RUNNING,
    GameSessionStatus.PAUSED: SessionLookupStatus.PAUSED,
    GameSessionStatus.ENDED: SessionLookupStatus.ENDED,
}


class InMemorySessionRegistry:
    """Process-local routing view; not suitable for production ownership."""

    def __init__(self) -> None:
        self._sessions_by_group: dict[str, SessionOwnershipSnapshot] = {}
        self._lock = RLock()

    def bind_session(self, session: GameSession) -> None:
        """Publish an existing session's routing snapshot without creating it."""

        try:
            lookup_status = _SESSION_STATUS_MAP[session.status]
        except KeyError as exc:
            raise ValueError("CREATED session does not own message routing") from exc

        active = lookup_status in {
            SessionLookupStatus.RUNNING,
            SessionLookupStatus.PAUSED,
        }
        snapshot = SessionOwnershipSnapshot(
            group_id=session.group_id,
            status=lookup_status,
            game_id=session.game_id if active else None,
            session_id=session.session_id if active else None,
            ownership_released=not active,
        )
        with self._lock:
            self._sessions_by_group[session.group_id] = snapshot

    def unbind_group(self, group_id: str) -> None:
        with self._lock:
            self._sessions_by_group.pop(group_id, None)

    def lookup(self, group_id: str) -> SessionOwnershipSnapshot:
        if not group_id:
            raise ValueError("group_id must not be empty")
        with self._lock:
            snapshot = self._sessions_by_group.get(group_id)
        if snapshot is not None:
            return snapshot
        return SessionOwnershipSnapshot(
            group_id=group_id,
            status=SessionLookupStatus.NO_SESSION,
        )
