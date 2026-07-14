"""Interface-only ports; P3-A provides no infrastructure implementations."""

from __future__ import annotations

from typing import Mapping, Protocol, Sequence

from game_runtime.action import GameAction, GameActionStatus
from game_runtime.event import GameEvent
from game_runtime.session import GameSession


class SessionRepository(Protocol):
    async def create_session(self, session: GameSession) -> None:
        """Persist a new Session snapshot."""

    async def get_session(self, game_id: str) -> GameSession | None:
        """Load a session by game_id."""

    async def update_session(
        self,
        session: GameSession,
        *,
        expected_state_version: int,
    ) -> None:
        """Persist one optimistic-concurrency Session update."""


class EventStore(Protocol):
    async def append_event(self, event: GameEvent) -> object:
        """Append one structured GameEvent."""

    async def get_events(self, game_id: str) -> Sequence[object]:
        """Return structured events for one game_id."""


class ActionExecutor(Protocol):
    async def execute(self, action: GameAction) -> GameActionStatus:
        """Execute an Action in a later phase; P3-A has no implementation."""


class AuditRecorder(Protocol):
    def record(
        self,
        *,
        game_id: str,
        event_type: str,
        fields: Mapping[str, object],
    ) -> None:
        """Record privacy-safe GAME-domain audit metadata."""
