"""Game Runtime domain skeleton.

P3-A intentionally exposes only in-memory domain models and interface contracts.
It has no LLM, QQ, persistence, or Normal Mode integration.
"""

from game_runtime.action import ActionQueue, GameAction, GameActionStatus
from game_runtime.actor import GameSessionActor
from game_runtime.event import GameEvent, GameEventSource, GameEventType
from game_runtime.identity import DMIdentity
from game_runtime.participant import ParticipantReference, ParticipantType
from game_runtime.session import GamePhase, GameSession, GameSessionStatus

__all__ = [
    "ActionQueue",
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
    "ParticipantReference",
    "ParticipantType",
]
