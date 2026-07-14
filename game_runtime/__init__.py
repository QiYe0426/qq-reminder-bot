"""Game Runtime domain contracts and platform-neutral routing surface.

Persistence and recovery remain explicit subpackages.  No LLM, QQ, production
message takeover, or Normal Mode integration is enabled here.
"""

from game_runtime.action import ActionQueue, GameAction, GameActionStatus
from game_runtime.actor import GameSessionActor
from game_runtime.event import GameEvent, GameEventSource, GameEventType
from game_runtime.identity import DMIdentity
from game_runtime.participant import (
    ParticipantMembershipState,
    ParticipantReference,
    ParticipantType,
)
from game_runtime.routing import (
    ActorGameRuntimeIngress,
    GameModeRouterAdapter,
    InMemorySessionRegistry,
    IngressMessageEnvelope,
    MessageType,
    ModeRouter,
    RouteDestination,
    SessionLookupStatus,
)
from game_runtime.session import GamePhase, GameSession, GameSessionStatus

__all__ = [
    "ActionQueue",
    "ActorGameRuntimeIngress",
    "DMIdentity",
    "GameAction",
    "GameActionStatus",
    "GameEvent",
    "GameEventSource",
    "GameEventType",
    "GamePhase",
    "GameSession",
    "GameSessionActor",
    "GameSessionStatus",
    "GameModeRouterAdapter",
    "InMemorySessionRegistry",
    "IngressMessageEnvelope",
    "MessageType",
    "ModeRouter",
    "ParticipantMembershipState",
    "ParticipantReference",
    "ParticipantType",
    "RouteDestination",
    "SessionLookupStatus",
]
