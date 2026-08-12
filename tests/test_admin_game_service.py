import asyncio

import pytest


def test_admin_game_service_creates_and_controls_session(tmp_path) -> None:
    from plugins.admin_game_service import AdminGameService

    async def scenario() -> None:
        service = AdminGameService(tmp_path / "game.db")
        created = await service.create_session(
            group_id="10001",
            dm_qq_id="20002",
            script_id="雾港疑云",
            players=[{"qq_id": "30003", "character_id": "侦探"}],
        )
        assert created["status"] == "CREATED"
        assert created["phase"] == "LOBBY"
        assert created["script_id"] == "雾港疑云"
        assert created["participants"] == 2
        assert await service.list_sessions() == [created]

        running = await service.control(created["game_id"], "start")
        assert running["status"] == "RUNNING"
        assert running["phase"] == "INTRODUCTION"

        exploration = await service.control(created["game_id"], "phase", phase="EXPLORATION")
        assert exploration["phase"] == "EXPLORATION"

        paused = await service.control(created["game_id"], "pause")
        assert paused["status"] == "PAUSED"
        sessions = await service.list_sessions()
        assert sessions == [paused]

    asyncio.run(scenario())


def test_admin_game_service_rejects_invalid_action(tmp_path) -> None:
    from plugins.admin_game_service import AdminGameService

    async def scenario() -> None:
        service = AdminGameService(tmp_path / "game.db")
        created = await service.create_session(
            group_id="10001",
            dm_qq_id="20002",
            script_id="雾港疑云",
            players=[{"qq_id": "30003", "character_id": "侦探"}],
        )
        with pytest.raises(ValueError, match="unsupported action"):
            await service.control(created["game_id"], "explode")

    asyncio.run(scenario())


def test_admin_game_service_requires_script_and_character_assignments(tmp_path) -> None:
    from plugins.admin_game_service import AdminGameService

    async def scenario() -> None:
        service = AdminGameService(tmp_path / "game.db")
        with pytest.raises(ValueError, match="script_id"):
            await service.create_session(group_id="10001", dm_qq_id="20002", script_id="", players=[])
        with pytest.raises(ValueError, match="player"):
            await service.create_session(group_id="10001", dm_qq_id="20002", script_id="script", players=[])

    asyncio.run(scenario())
