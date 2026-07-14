import asyncio

import pytest

from game_runtime.errors import PersistenceConflict
from game_runtime.persistence import (
    GameSessionRepository,
    ParticipantRepository,
    SQLiteGameDatabase,
)
from game_runtime.session import GameSessionStatus


def run(coroutine):
    return asyncio.run(coroutine)


def test_session_save_and_read_preserves_snapshot(session_factory, tmp_path) -> None:
    async def scenario() -> None:
        database = SQLiteGameDatabase(tmp_path / "game.db")
        await database.initialize()
        repository = GameSessionRepository(database)
        session = session_factory()

        await repository.create_session(session)
        restored = await repository.get_session(session.game_id)

        assert restored is not None
        assert restored.game_id == session.game_id
        assert restored.session_id == session.session_id
        assert restored.group_id == session.group_id
        assert restored.status is session.status
        assert restored.current_phase is session.current_phase
        assert restored.state_version == session.state_version
        assert restored.created_at == session.created_at
        assert restored.updated_at == session.updated_at
        assert restored.participant_references == session.participant_references

    run(scenario())


def test_game_scoped_reads_do_not_cross_sessions(session_factory, tmp_path) -> None:
    async def scenario() -> None:
        database = SQLiteGameDatabase(tmp_path / "game.db")
        await database.initialize()
        sessions = GameSessionRepository(database)
        participants = ParticipantRepository(database)
        game_a = session_factory("game-a", "group-a")

        await sessions.create_session(game_a)

        assert await sessions.get_session("game-b") is None
        assert await participants.get_participants("game-b") == ()
        assert await participants.get_participants("game-a") == tuple(
            sorted(
                game_a.participant_references,
                key=lambda participant: participant.participant_id,
            )
        )

    run(scenario())


def test_active_group_ownership_is_unique_and_persisted(
    session_factory,
    tmp_path,
) -> None:
    async def scenario() -> None:
        database = SQLiteGameDatabase(tmp_path / "game.db")
        await database.initialize()
        repository = GameSessionRepository(database)
        first = session_factory("game-a", "shared-group")
        second = session_factory("game-b", "shared-group")
        await repository.create_session(first)
        await repository.create_session(second)

        first.transition_to(GameSessionStatus.RUNNING)
        await repository.update_session(first, expected_state_version=0)
        ownership = await repository.get_ownership("shared-group")
        assert ownership is not None
        assert ownership.game_id == "game-a"

        second.transition_to(GameSessionStatus.RUNNING)
        with pytest.raises(PersistenceConflict):
            await repository.update_session(second, expected_state_version=0)

        restored_second = await repository.get_session("game-b")
        assert restored_second is not None
        assert restored_second.status is GameSessionStatus.CREATED

    run(scenario())
