"""P3-B platform-neutral Mode Router integration."""

from game_runtime.routing.adapter import GameModeRouterAdapter
from game_runtime.routing.contracts import (
    GameRuntimeIngress,
    GlobalControlChecker,
    SessionRegistry,
)
from game_runtime.routing.factory import GameEventFactory
from game_runtime.routing.ingress import ActorGameRuntimeIngress
from game_runtime.routing.models import (
    GlobalControlResult,
    IngressMessageEnvelope,
    MessageType,
    RouteDecision,
    RouteDestination,
    RouteFailureReason,
    RoutedGameMessageEnvelope,
    RouterAdapterResult,
    SessionLookupStatus,
    SessionOwnershipSnapshot,
)
from game_runtime.routing.registry import InMemorySessionRegistry
from game_runtime.routing.router import ModeRouter, NoGlobalControlChecker

__all__ = [
    "ActorGameRuntimeIngress",
    "GameEventFactory",
    "GameModeRouterAdapter",
    "GameRuntimeIngress",
    "GlobalControlChecker",
    "GlobalControlResult",
    "InMemorySessionRegistry",
    "IngressMessageEnvelope",
    "MessageType",
    "ModeRouter",
    "NoGlobalControlChecker",
    "RouteDecision",
    "RouteDestination",
    "RouteFailureReason",
    "RoutedGameMessageEnvelope",
    "RouterAdapterResult",
    "SessionLookupStatus",
    "SessionOwnershipSnapshot",
    "SessionRegistry",
]
