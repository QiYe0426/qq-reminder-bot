import asyncio

from game_runtime.persistence import CURRENT_SCHEMA_VERSION, SQLiteGameDatabase


def run(coroutine):
    return asyncio.run(coroutine)


def test_database_initialization_is_idempotent_and_versioned(tmp_path) -> None:
    async def scenario() -> None:
        database = SQLiteGameDatabase(tmp_path / "game.db")

        await database.initialize()
        await database.initialize()

        assert await database.schema_version() == CURRENT_SCHEMA_VERSION

    run(scenario())
