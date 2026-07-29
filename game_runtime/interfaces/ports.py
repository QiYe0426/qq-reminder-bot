"""Interface-only ports; P3-A provides no infrastructure implementations."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Mapping, Protocol, Sequence, runtime_checkable

from game_runtime.action import GameAction, GameActionStatus
from game_runtime.event import GameEvent
from game_runtime.session import GameSession

if TYPE_CHECKING:
    from game_runtime.session_control.apply_contract import (
        ControlApplyPlan,
        ControlApplyReceipt,
        ControlOperationClaim,
        ControlRejectPlan,
        CreateSessionWithEventPlan,
    )


class SessionRepository(Protocol):
    async def create_session(self, session: GameSession) -> None:
        """Persist a new Session snapshot."""

    async def get_session(self, game_id: str) -> GameSession | None:
        """Load a session by game_id."""

    async def list_sessions_by_group(
        self,
        group_id: str,
    ) -> Sequence[GameSession]:
        """Load Session snapshots belonging to one group."""

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


@runtime_checkable
class SessionControlApplyPort(Protocol):
    """Actor-only atomic commit boundary; this module provides no adapter."""

    async def claim_operation(
        self,
        *,
        game_id: str,
        session_id: str,
        command_id: str,
        operation_id: str,
        input_event_id: str,
        claim_id: str,
        claimed_at: datetime,
    ) -> "ControlOperationClaim":
        """CAS claim a CREATED Operation before an Apply attempt."""

    async def commit_control_apply(
        self,
        plan: "ControlApplyPlan",
        claim: "ControlOperationClaim",
    ) -> "ControlApplyReceipt":
        """Atomically commit State, Event processing, results, and Operation."""

    async def commit_control_rejection(
        self,
        plan: "ControlRejectPlan",
        claim: "ControlOperationClaim",
    ) -> "ControlApplyReceipt":
        """Atomically reject input without changing business State."""

    async def create_session_with_event(
        self,
        plan: "CreateSessionWithEventPlan",
    ) -> "ControlApplyReceipt":
        """Bootstrap contract only; P3-D-6.4 defines its plan and adapter."""
