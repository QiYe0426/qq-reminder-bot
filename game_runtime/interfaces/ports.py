"""Interface-only ports; P3-A provides no infrastructure implementations."""

from __future__ import annotations

from typing import Mapping, Protocol, Sequence

from game_runtime.action import GameAction, GameActionStatus
from game_runtime.event import GameEvent
from game_runtime.session import GameSession


class SessionRepository(Protocol):
    def get(self, game_id: str) -> GameSession | None:
        """Load a session by game_id."""

    def save(self, session: GameSession) -> None:
        """Save a session aggregate."""


class EventStore(Protocol):
    def append(self, event: GameEvent) -> None:
        """Append one structured GameEvent."""

    def list_for_game(self, game_id: str) -> Sequence[GameEvent]:
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
