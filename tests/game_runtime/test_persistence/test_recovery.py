import asyncio

import pytest

from game_runtime.errors import ActorOwnershipConflict
from game_runtime.persistence import (
    GameActionRepository,
    GameEventRepository,
    GameSessionRepository,
    RuntimeStateRepository,
    SQLiteGameDatabase,
)
from game_runtime.recovery import (
    ActorOwnershipRegistry,
    RecoveryDisposition,
    RecoveryManager,
)
from game_runtime.session import GameSessionStatus


def run(coroutine):
    return asyncio.run(coroutine)


def make_manager(database, ownership=None):
    return RecoveryManager(
        sessions=GameSessionRepository(database),
        events=GameEventRepository(database),
        actions=GameActionRepository(database),
        runtime_state=RuntimeStateRepository(database),
        actor_ownership=(
            ownership if ownership is not None else ActorOwnershipRegistry()
        ),
    )


async def persist_running_session(database, session) -> None:
    repository = GameSessionRepository(database)
    await repository.create_session(session)
    session.transition_to(GameSessionStatus.RUNNING)
    await repository.update_session(session, expected_state_version=0)


def test_clean_restart_recovers_running_session_and_actor(
    session_factory,
    tmp_path,
) -> None:
    async def scenario() -> None:
        database = SQLiteGameDatabase(tmp_path / "game.db")
        await database.initialize()
        session = session_factory()
        await persist_running_session(database, session)
        runtime_state = RuntimeStateRepository(database)
        await runtime_state.begin_runtime()
        await runtime_state.mark_clean_shutdown()

        ownership = ActorOwnershipRegistry()
        result = await make_manager(database, ownership).recover()

        assert result.previous_shutdown_clean
        assert not result.safe_mode
        assert len(result.sessions) == 1
        recovered = result.sessions[0]
        assert recovered.disposition is RecoveryDisposition.RESUMED
        assert recovered.session_status is GameSessionStatus.RUNNING
        assert recovered.actor is ownership.get_owner("game-1")

    run(scenario())


def test_unclean_restart_pauses_running_session(session_factory, tmp_path) -> None:
    async def scenario() -> None:
        database = SQLiteGameDatabase(tmp_path / "game.db")
        await database.initialize()
        await persist_running_session(database, session_factory())

        result = await make_manager(database).recover()

        assert not result.previous_shutdown_clean
        assert result.sessions[0].disposition is RecoveryDisposition.PAUSED
        assert result.sessions[0].session_status is GameSessionStatus.PAUSED
        restored = await GameSessionRepository(database).get_session("game-1")
        assert restored is not None
        assert restored.status is GameSessionStatus.PAUSED

    run(scenario())


def test_database_failure_enters_safe_mode_without_running_actor(tmp_path) -> None:
    async def scenario() -> None:
        database = SQLiteGameDatabase(tmp_path / "not-initialized.db")

        result = await make_manager(database).recover()

        assert result.safe_mode
        assert result.sessions == ()
        assert result.error_code == "PERSISTENCE_UNAVAILABLE"

    run(scenario())


def test_actor_ownership_conflict_fails(session_factory) -> None:
    from game_runtime.actor import GameSessionActor

    registry = ActorOwnershipRegistry()
    first = GameSessionActor(session_factory())
    second = GameSessionActor(session_factory())

    registry.acquire_session_owner(first)
    with pytest.raises(ActorOwnershipConflict):
        registry.acquire_session_owner(second)

    assert registry.get_owner("game-1") is first
