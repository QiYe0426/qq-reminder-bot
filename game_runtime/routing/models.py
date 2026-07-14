"""Platform-neutral routing contracts for P3-B."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Mapping

from game_runtime.event import GameEvent


class MessageType(str, Enum):
    GROUP_TEXT = "GROUP_TEXT"


class SessionLookupStatus(str, Enum):
    NO_SESSION = "NO_SESSION"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    ENDED = "ENDED"


class RouteDestination(str, Enum):
    GLOBAL_CONTROL = "GLOBAL_CONTROL"
    GAME = "GAME"
    NORMAL = "NORMAL"
    REJECT = "REJECT"


class GlobalControlResult(str, Enum):
    NOT_CONTROL = "NOT_CONTROL"
    AUTHORIZED = "AUTHORIZED"
    DENIED = "DENIED"
    ERROR = "ERROR"


class RouteFailureReason(str, Enum):
    NONE = "NONE"
    GLOBAL_CONTROL_DENIED = "GLOBAL_CONTROL_DENIED"
    GLOBAL_CONTROL_ERROR = "GLOBAL_CONTROL_ERROR"
    REGISTRY_ERROR = "REGISTRY_ERROR"
    INVALID_REGISTRY_RESULT = "INVALID_REGISTRY_RESULT"
    GAME_EVENT_ERROR = "GAME_EVENT_ERROR"
    GAME_RUNTIME_UNAVAILABLE = "GAME_RUNTIME_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class IngressMessageEnvelope:
    """Normalized input before any game/session scope is trusted."""

    platform_event_id: str
    group_id: str
    sender_id: str
    timestamp: datetime
    message_type: MessageType
    correlation_id: str
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (
            ("platform_event_id", self.platform_event_id),
            ("group_id", self.group_id),
            ("sender_id", self.sender_id),
            ("correlation_id", self.correlation_id),
        ):
            if not value:
                raise ValueError(f"{name} must not be empty")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class SessionOwnershipSnapshot:
    """Atomic, read-only group ownership result from Session Registry."""

    group_id: str
    status: SessionLookupStatus
    game_id: str | None = None
    session_id: str | None = None
    ownership_released: bool = True

    def __post_init__(self) -> None:
        if not self.group_id:
            raise ValueError("group_id must not be empty")
        active = self.status in {
            SessionLookupStatus.RUNNING,
            SessionLookupStatus.PAUSED,
        }
        if active:
            if not self.game_id or not self.session_id:
                raise ValueError("active ownership requires game_id and session_id")
            if self.ownership_released:
                raise ValueError("active ownership cannot be marked released")
            return
        if self.game_id is not None or self.session_id is not None:
            raise ValueError("inactive ownership cannot expose game/session scope")
        if not self.ownership_released:
            raise ValueError("inactive ownership must be marked released")


@dataclass(frozen=True, slots=True)
class RoutedGameMessageEnvelope:
    """Message enriched only with a trusted active Session snapshot."""

    message: IngressMessageEnvelope
    game_id: str
    session_id: str
    session_status: SessionLookupStatus

    def __post_init__(self) -> None:
        if not self.game_id or not self.session_id:
            raise ValueError("routed game message requires game_id and session_id")
        if self.session_status not in {
            SessionLookupStatus.RUNNING,
            SessionLookupStatus.PAUSED,
        }:
            raise ValueError("routed game message requires active session status")


@dataclass(frozen=True, slots=True)
class RouteDecision:
    destination: RouteDestination
    reason: RouteFailureReason = RouteFailureReason.NONE
    session: SessionOwnershipSnapshot | None = None

    def __post_init__(self) -> None:
        if self.destination is RouteDestination.GAME:
            if self.session is None or self.session.status not in {
                SessionLookupStatus.RUNNING,
                SessionLookupStatus.PAUSED,
            }:
                raise ValueError("GAME decision requires active session ownership")
        if self.destination is RouteDestination.REJECT:
            if self.reason is RouteFailureReason.NONE:
                raise ValueError("REJECT decision requires a failure reason")
        elif self.reason is not RouteFailureReason.NONE:
            raise ValueError("non-REJECT decision cannot carry failure reason")


@dataclass(frozen=True, slots=True)
class RouterAdapterResult:
    decision: RouteDecision
    game_event: GameEvent | None = None
    delivered: bool = False
