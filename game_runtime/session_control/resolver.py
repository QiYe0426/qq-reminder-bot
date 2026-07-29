"""Read-only Session resolution and CREATE bootstrap context for P3-D-2."""

from __future__ import annotations

from dataclasses import dataclass

from game_runtime.errors import GameRuntimeError
from game_runtime.interfaces import SessionRepository
from game_runtime.session import GameSession, GameSessionStatus
from game_runtime.session_control.bootstrap import (
    BootstrapIdFactory,
    CreateSessionBootstrapContext,
)
from game_runtime.session_control.commands import SessionCommand, SessionCommandType


_OPEN_GROUP_SESSION_STATUSES = frozenset(
    {
        GameSessionStatus.CREATED,
        GameSessionStatus.RUNNING,
        GameSessionStatus.PAUSED,
    }
)


class SessionResolutionError(GameRuntimeError):
    """Base error for fail-closed Session Control scope resolution."""


class SessionNotFound(SessionResolutionError):
    """Raised when a scoped non-CREATE command has no Session snapshot."""


class SessionScopeMismatch(SessionResolutionError):
    """Raised when resolved game/session/group identifiers disagree."""


class AmbiguousGroupSession(SessionResolutionError):
    """Raised when a group has more than one non-ended Session."""


@dataclass(frozen=True, slots=True)
class ExistingSessionContext:
    """A command paired with its read-only authoritative Session snapshot."""

    command: SessionCommand
    session: GameSession


SessionResolution = ExistingSessionContext | CreateSessionBootstrapContext


class GroupSessionResolver:
    """Read-only lookup for one group's CREATED/RUNNING/PAUSED Session."""

    def __init__(self, sessions: SessionRepository) -> None:
        self._sessions = sessions

    async def find_open_session(self, group_id: str) -> GameSession | None:
        if not isinstance(group_id, str):
            raise TypeError("group_id must be a string")
        if not group_id.strip():
            raise ValueError("group_id must not be empty")

        candidates = tuple(
            session
            for session in await self._sessions.list_sessions_by_group(group_id)
            if session.status in _OPEN_GROUP_SESSION_STATUSES
        )
        if len(candidates) > 1:
            raise AmbiguousGroupSession(
                "group has more than one non-ended Session"
            )
        return candidates[0] if candidates else None


class SessionResolver:
    """Resolve a validated command without mutating Session or persistence."""

    def __init__(
        self,
        sessions: SessionRepository,
        bootstrap_ids: BootstrapIdFactory | None = None,
    ) -> None:
        self._sessions = sessions
        self._groups = GroupSessionResolver(sessions)
        self._bootstrap_ids = bootstrap_ids or BootstrapIdFactory()

    async def resolve(self, command: SessionCommand) -> SessionResolution:
        if not isinstance(command, SessionCommand):
            raise TypeError("command must be a SessionCommand")

        if command.command_type is SessionCommandType.CREATE_SESSION:
            existing = await self._groups.find_open_session(command.group_id)
            if existing is not None:
                return ExistingSessionContext(command=command, session=existing)
            proposed_ids = self._bootstrap_ids.generate()
            return CreateSessionBootstrapContext(
                group_id=command.group_id,
                requester=command.requester,
                command_id=command.command_id,
                correlation_id=command.correlation_id,
                requested_at=command.requested_at,
                proposed_game_id=proposed_ids.game_id,
                proposed_session_id=proposed_ids.session_id,
            )

        game_id = command.game_id
        session_id = command.session_id
        if game_id is None or session_id is None:
            raise SessionScopeMismatch("non-CREATE command requires Session scope")

        session = await self._sessions.get_session(game_id)
        if session is None:
            raise SessionNotFound("Session does not exist")
        if session.game_id != game_id:
            raise SessionScopeMismatch("resolved game_id does not match command")
        if session.session_id != session_id:
            raise SessionScopeMismatch("resolved session_id does not match command")
        if session.group_id != command.group_id:
            raise SessionScopeMismatch("resolved group_id does not match command")
        return ExistingSessionContext(command=command, session=session)
