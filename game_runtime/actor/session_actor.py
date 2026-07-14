"""In-memory, single-writer GameSession actor skeleton."""

from __future__ import annotations

from collections import deque
from threading import RLock
from typing import Callable

from game_runtime.errors import EventSessionMismatch
from game_runtime.event import GameEvent
from game_runtime.session import GameSession

StateUpdater = Callable[[GameSession, GameEvent], None]


class GameSessionActor:
    """Serializes events for exactly one game_id.

    P3-A drains an in-memory mailbox synchronously under a per-actor writer lock.
    It performs no I/O and owns no background thread or process.
    """

    def __init__(
        self,
        session: GameSession,
        *,
        state_updater: StateUpdater | None = None,
    ) -> None:
        self._session = session
        self._state_updater = state_updater
        self._mailbox: deque[GameEvent] = deque()
        self._seen_event_ids: set[str] = set()
        self._processed_event_ids: list[str] = []
        self._writer_lock = RLock()

    @property
    def game_id(self) -> str:
        return self._session.game_id

    @property
    def session_id(self) -> str:
        return self._session.session_id

    @property
    def session(self) -> GameSession:
        return self._session

    @property
    def processed_event_ids(self) -> tuple[str, ...]:
        return tuple(self._processed_event_ids)

    @property
    def pending_count(self) -> int:
        return len(self._mailbox)

    def receive(self, event: GameEvent) -> bool:
        """Accept and serially process an event.

        Returns False for an already-seen event ID. Cross-session delivery is
        always rejected.
        """

        self._validate_event_scope(event)
        with self._writer_lock:
            if event.event_id in self._seen_event_ids:
                return False
            self._seen_event_ids.add(event.event_id)
            self._mailbox.append(event)
            self._drain_mailbox()
            return True

    def process(self, event: GameEvent) -> None:
        """Process one event while preserving the actor's single-writer lock."""

        self._validate_event_scope(event)
        with self._writer_lock:
            self.update_state(event)
            self._processed_event_ids.append(event.event_id)

    def update_state(self, event: GameEvent) -> None:
        """Invoke the optional pure state-update hook.

        No game logic is provided in P3-A. Later phases may bind a domain
        handler without allowing event producers to mutate the session.
        """

        if self._state_updater is not None:
            self._state_updater(self._session, event)

    def _drain_mailbox(self) -> None:
        while self._mailbox:
            event = self._mailbox.popleft()
            try:
                self.process(event)
            except Exception:
                self._seen_event_ids.discard(event.event_id)
                raise

    def _validate_event_scope(self, event: GameEvent) -> None:
        if event.game_id != self.game_id or event.session_id != self.session_id:
            raise EventSessionMismatch(
                f"event scope ({event.game_id!r}, {event.session_id!r}) does not "
                f"match actor scope ({self.game_id!r}, {self.session_id!r})"
            )
