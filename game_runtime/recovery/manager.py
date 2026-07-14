"""Fail-closed recovery of persisted Game Runtime sessions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from game_runtime.actor import GameSessionActor
from game_runtime.errors import PersistenceError
from game_runtime.persistence import (
    EventProcessingStatus,
    GameActionRepository,
    GameEventRepository,
    GameSessionRepository,
    RuntimeStateRepository,
)
from game_runtime.recovery.ownership import ActorOwnershipRegistry
from game_runtime.session import GameSession, GameSessionStatus


class RecoveryDisposition(str, Enum):
    RESUMED = "RESUMED"
    PAUSED = "PAUSED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class SessionRecoveryResult:
    game_id: str
    disposition: RecoveryDisposition
    session_status: GameSessionStatus
    actor: GameSessionActor | None
    recovered_unknown_actions: int = 0
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveryBatchResult:
    previous_shutdown_clean: bool
    safe_mode: bool
    sessions: tuple[SessionRecoveryResult, ...]
    error_code: str | None = None


class RecoveryManager:
    """Rebuilds Actors only from validated, game-scoped persistence facts."""

    def __init__(
        self,
        *,
        sessions: GameSessionRepository,
        events: GameEventRepository,
        actions: GameActionRepository,
        runtime_state: RuntimeStateRepository,
        actor_ownership: ActorOwnershipRegistry,
    ) -> None:
        self._sessions = sessions
        self._events = events
        self._actions = actions
        self._runtime_state = runtime_state
        self._actor_ownership = actor_ownership

    async def recover(self) -> RecoveryBatchResult:
        try:
            previous_shutdown_clean = await self._runtime_state.begin_runtime()
            active_sessions = await self._sessions.list_active_sessions()
        except Exception:
            return RecoveryBatchResult(
                previous_shutdown_clean=False,
                safe_mode=True,
                sessions=(),
                error_code="PERSISTENCE_UNAVAILABLE",
            )

        results: list[SessionRecoveryResult] = []
        for session in active_sessions:
            results.append(
                await self._recover_session(
                    session,
                    previous_shutdown_clean=previous_shutdown_clean,
                )
            )
        return RecoveryBatchResult(
            previous_shutdown_clean=previous_shutdown_clean,
            safe_mode=any(
                result.disposition is RecoveryDisposition.FAILED
                for result in results
            ),
            sessions=tuple(results),
        )

    async def _recover_session(
        self,
        session: GameSession,
        *,
        previous_shutdown_clean: bool,
    ) -> SessionRecoveryResult:
        actor_acquired = False
        try:
            await self._validate_persistence(session)
            recovered_unknown_actions = (
                await self._actions.recover_executing_as_unknown(session.game_id)
            )
            if (
                not previous_shutdown_clean
                and session.status is GameSessionStatus.RUNNING
            ):
                await self._pause_session(session)

            actor = GameSessionActor(session)
            self._actor_ownership.acquire_session_owner(actor)
            actor_acquired = True
            disposition = (
                RecoveryDisposition.RESUMED
                if session.status is GameSessionStatus.RUNNING
                else RecoveryDisposition.PAUSED
            )
            return SessionRecoveryResult(
                game_id=session.game_id,
                disposition=disposition,
                session_status=session.status,
                actor=actor,
                recovered_unknown_actions=recovered_unknown_actions,
            )
        except Exception as exc:
            if actor_acquired:
                self._actor_ownership.release_session_owner(session.game_id)
            await self._best_effort_pause(session)
            return SessionRecoveryResult(
                game_id=session.game_id,
                disposition=RecoveryDisposition.FAILED,
                session_status=session.status,
                actor=None,
                error_code=type(exc).__name__.upper(),
            )

    async def _validate_persistence(self, session: GameSession) -> None:
        ownership = await self._sessions.get_ownership(session.group_id)
        if ownership is None:
            raise PersistenceError("active session ownership is missing")
        if (
            ownership.game_id != session.game_id
            or ownership.session_id != session.session_id
            or ownership.session_status.value != session.status.value
            or ownership.state_version != session.state_version
        ):
            raise PersistenceError("active session ownership is inconsistent")

        events = await self._events.get_events(session.game_id)
        expected_sequence = 1
        highest_applied = 0
        encountered_unapplied = False
        for stored_event in events:
            if stored_event.sequence_no != expected_sequence:
                raise PersistenceError("event sequence is not contiguous")
            expected_sequence += 1
            if stored_event.processing_status is EventProcessingStatus.APPLIED:
                if encountered_unapplied:
                    raise PersistenceError("applied events are not a contiguous prefix")
                highest_applied = stored_event.sequence_no
                if stored_event.applied_state_version is None:
                    raise PersistenceError("applied event lacks a state version")
            else:
                encountered_unapplied = True
        if session.last_applied_sequence_no != highest_applied:
            raise PersistenceError("session event cursor is inconsistent")

    async def _pause_session(self, session: GameSession) -> None:
        if session.status is not GameSessionStatus.RUNNING:
            return
        expected_version = session.state_version
        session.transition_to(GameSessionStatus.PAUSED)
        await self._sessions.update_session(
            session,
            expected_state_version=expected_version,
        )

    async def _best_effort_pause(self, session: GameSession) -> None:
        if session.status is not GameSessionStatus.RUNNING:
            return
        try:
            await self._pause_session(session)
        except Exception:
            # Recovery result remains fail closed even if storage cannot record PAUSED.
            return
