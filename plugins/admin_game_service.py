"""Read-only admin projection over formally persisted Game Runtime sessions."""

from __future__ import annotations

from pathlib import Path

from game_runtime.persistence.database import DEFAULT_GAME_DATABASE_PATH, SQLiteGameDatabase
from game_runtime.persistence.repositories.session import GameSessionRepository
from game_runtime.session import GameSession


class AdminGameService:
    def __init__(self, database_path: str | Path = DEFAULT_GAME_DATABASE_PATH) -> None:
        self.database = SQLiteGameDatabase(database_path)
        self.sessions = GameSessionRepository(self.database)

    async def list_sessions(self) -> list[dict[str, object]]:
        await self.database.initialize()
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT game_id FROM game_sessions WHERE status != 'ENDED' "
                "ORDER BY updated_at DESC, game_id"
            )
            game_ids = [str(row["game_id"]) for row in await cursor.fetchall()]
        result = []
        for game_id in game_ids:
            session = await self.sessions.get_session(game_id)
            if session is not None:
                result.append(self.serialize(session))
        return result

    @staticmethod
    def serialize(session: GameSession) -> dict[str, object]:
        return {
            "game_id": session.game_id,
            "session_id": session.session_id,
            "group_id": session.group_id,
            "dm_qq_id": session.dm_identity.qq_id,
            "status": session.status.value,
            "phase": session.current_phase.value,
            "state_version": session.state_version,
            "participants": len(session.participant_references),
            "updated_at": session.updated_at.isoformat(),
        }
