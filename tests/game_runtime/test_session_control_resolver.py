import asyncio
from collections.abc import Sequence
from datetime import datetime, timezone

import pytest

from game_runtime.session import GameSession, GameSessionStatus
from game_runtime.session_control import (
    AmbiguousGroupSession,
    BootstrapIdFactory,
    CreateSessionBootstrapContext,
    CreateSessionPayload,
    ExistingSessionContext,
    SessionCommand,
    SessionCommandType,
    SessionNotFound,
    SessionResolver,
    SessionScopeMismatch,
    StartGamePayload,
)
def run(coroutine):
    return asyncio.run(coroutine)


REQUESTED_AT = datetime(2026, 7, 15, tzinfo=timezone.utc)


class InMemorySessionQueryPort:
    """Read-only test double; writes fail if the resolver attempts one."""

    def __init__(self, sessions: Sequence[GameSession] = ()) -> None:
        self._sessions = tuple(sessions)

    async def create_session(self, session: GameSession) -> None:
        raise AssertionError("SessionResolver must not create a Session")

    async def get_session(self, game_id: str) -> GameSession | None:
        return next(
            (session for session in self._sessions if session.game_id == game_id),
            None,
        )

    async def list_sessions_by_group(
        self,
        group_id: str,
    ) -> tuple[GameSession, ...]:
        return tuple(
            session for session in self._sessions if session.group_id == group_id
        )

    async def update_session(
        self,
        session: GameSession,
        *,
        expected_state_version: int,
    ) -> None:
        raise AssertionError("SessionResolver must not update a Session")


def make_create_command(group_id: str = "group-1") -> SessionCommand:
    return SessionCommand(
        command_id="command-create",
        command_type=SessionCommandType.CREATE_SESSION,
        requester="principal-dm-1",
        group_id=group_id,
        game_id=None,
        session_id=None,
        requester_binding_version=None,
        observed_state_version=None,
        payload=CreateSessionPayload(prospective_dm_id="participant-dm-1"),
        correlation_id="correlation-create",
        requested_at=REQUESTED_AT,
    )


def make_start_command(session: GameSession, **scope: object) -> SessionCommand:
    values: dict[str, object] = {
        "game_id": session.game_id,
        "session_id": session.session_id,
        "group_id": session.group_id,
    }
    values.update(scope)
    return SessionCommand(
        command_id="command-start",
        command_type=SessionCommandType.START_GAME,
        requester="principal-dm-1",
        game_id=values["game_id"],  # type: ignore[arg-type]
        session_id=values["session_id"],  # type: ignore[arg-type]
        group_id=values["group_id"],  # type: ignore[arg-type]
        requester_binding_version=1,
        observed_state_version=session.state_version,
        payload=StartGamePayload(),
        correlation_id="correlation-start",
        requested_at=REQUESTED_AT,
    )


def test_existing_session_is_resolved_without_mutation(session_factory) -> None:
    session = session_factory("game-existing", "group-existing")
    resolver = SessionResolver(InMemorySessionQueryPort((session,)))

    result = run(resolver.resolve(make_start_command(session)))

    assert isinstance(result, ExistingSessionContext)
    assert result.session is session
    assert session.status is GameSessionStatus.CREATED
    assert session.state_version == 0


def test_group_without_session_returns_bootstrap_context() -> None:
    resolver = SessionResolver(
        InMemorySessionQueryPort(),
        BootstrapIdFactory(
            lambda: "game-proposed",
            lambda: "session-proposed",
        ),
    )

    result = run(resolver.resolve(make_create_command()))

    assert result == CreateSessionBootstrapContext(
        group_id="group-1",
        requester="principal-dm-1",
        command_id="command-create",
        correlation_id="correlation-create",
        requested_at=REQUESTED_AT,
        proposed_game_id="game-proposed",
        proposed_session_id="session-proposed",
    )


@pytest.mark.parametrize(
    "status",
    [
        GameSessionStatus.CREATED,
        GameSessionStatus.RUNNING,
        GameSessionStatus.PAUSED,
    ],
)
def test_duplicate_create_resolves_existing_open_session(
    session_factory,
    status: GameSessionStatus,
) -> None:
    session = session_factory("game-existing", "group-1")
    if status is GameSessionStatus.RUNNING:
        session.transition_to(GameSessionStatus.RUNNING)
    elif status is GameSessionStatus.PAUSED:
        session.transition_to(GameSessionStatus.RUNNING)
        session.transition_to(GameSessionStatus.PAUSED)
    resolver = SessionResolver(InMemorySessionQueryPort((session,)))

    result = run(resolver.resolve(make_create_command()))

    assert isinstance(result, ExistingSessionContext)
    assert result.session is session


def test_ended_session_does_not_block_create_bootstrap(session_factory) -> None:
    session = session_factory("game-ended", "group-1")
    session.transition_to(GameSessionStatus.ENDED)
    resolver = SessionResolver(InMemorySessionQueryPort((session,)))

    result = run(resolver.resolve(make_create_command()))

    assert isinstance(result, CreateSessionBootstrapContext)


def test_more_than_one_open_group_session_fails_closed(session_factory) -> None:
    first = session_factory("game-a", "group-1")
    second = session_factory("game-b", "group-1")
    resolver = SessionResolver(InMemorySessionQueryPort((first, second)))

    with pytest.raises(AmbiguousGroupSession):
        run(resolver.resolve(make_create_command()))


@pytest.mark.parametrize(
    ("scope", "error"),
    [
        ({"session_id": "session-other"}, "session_id"),
        ({"group_id": "group-other"}, "group_id"),
    ],
)
def test_invalid_existing_session_scope_is_rejected(
    session_factory,
    scope: dict[str, object],
    error: str,
) -> None:
    session = session_factory("game-existing", "group-existing")
    resolver = SessionResolver(InMemorySessionQueryPort((session,)))

    with pytest.raises(SessionScopeMismatch, match=error):
        run(resolver.resolve(make_start_command(session, **scope)))


def test_missing_game_scope_is_rejected(session_factory) -> None:
    session = session_factory("game-existing", "group-existing")
    resolver = SessionResolver(InMemorySessionQueryPort())

    with pytest.raises(SessionNotFound):
        run(resolver.resolve(make_start_command(session)))
