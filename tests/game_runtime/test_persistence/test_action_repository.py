import asyncio

from game_runtime.action import GameAction, GameActionStatus
from game_runtime.persistence import (
    GameActionRepository,
    GameSessionRepository,
    SQLiteGameDatabase,
)


def run(coroutine):
    return asyncio.run(coroutine)


def test_executing_action_recovers_as_unknown_and_stays_unknown(
    session_factory,
    tmp_path,
) -> None:
    async def scenario() -> None:
        database = SQLiteGameDatabase(tmp_path / "game.db")
        await database.initialize()
        await GameSessionRepository(database).create_session(session_factory())
        repository = GameActionRepository(database)
        action = GameAction(
            action_id="action-1",
            game_id="game-1",
            session_id="session-game-1",
            action_type="SPEAK_PUBLIC",
        )
        await repository.create_action(action)
        await repository.transition_action(
            "game-1",
            "action-1",
            expected_status=GameActionStatus.CREATED,
            target_status=GameActionStatus.EXECUTING,
        )

        assert await repository.recover_executing_as_unknown("game-1") == 1
        assert await repository.recover_executing_as_unknown("game-1") == 0
        restored = await GameActionRepository(database).get_action(
            "game-1",
            "action-1",
        )
        assert restored is not None
        assert restored.status is GameActionStatus.UNKNOWN

    run(scenario())
