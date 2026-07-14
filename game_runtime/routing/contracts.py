"""Interface contracts used by the P3-B routing layer."""

from typing import Protocol

from game_runtime.event import GameEvent
from game_runtime.routing.models import (
    GlobalControlResult,
    IngressMessageEnvelope,
    SessionOwnershipSnapshot,
)


class SessionRegistry(Protocol):
    def lookup(self, group_id: str) -> SessionOwnershipSnapshot:
        """Return the current atomic ownership snapshot for one group."""


class GlobalControlChecker(Protocol):
    def check(self, message: IngressMessageEnvelope) -> GlobalControlResult:
        """Classify and authenticate the dedicated global control plane."""


class GameRuntimeIngress(Protocol):
    def accept(self, event: GameEvent) -> bool:
        """Submit an in-memory GameEvent to its session actor."""
