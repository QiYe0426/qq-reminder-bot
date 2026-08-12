import asyncio


def test_admin_game_service_lists_runtime_sessions_without_mutating_them(tmp_path) -> None:
    from game_runtime.identity import DMIdentity
    from game_runtime.persistence.database import SQLiteGameDatabase
    from game_runtime.persistence.repositories.session import GameSessionRepository
    from game_runtime.session import GameSession
    from plugins.admin_game_service import AdminGameService

    async def scenario() -> None:
        database = SQLiteGameDatabase(tmp_path / "game.db")
        await database.initialize()
        repository = GameSessionRepository(database)
        session = GameSession(
            game_id="game-1",
            session_id="session-1",
            group_id="10001",
            dm_identity=DMIdentity(participant_id="dm-1", qq_id="20002"),
        )
        await repository.create_session(session)

        service = AdminGameService(tmp_path / "game.db")
        assert await service.list_sessions() == [{
            "game_id": "game-1",
            "session_id": "session-1",
            "group_id": "10001",
            "dm_qq_id": "20002",
            "status": "CREATED",
            "phase": "LOBBY",
            "state_version": 0,
            "participants": 0,
            "updated_at": session.updated_at.isoformat(),
        }]

    asyncio.run(scenario())
