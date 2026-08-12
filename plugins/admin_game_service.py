"""Small admin-facing facade over the persistent Game Runtime aggregate."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from game_runtime.identity import DMIdentity
from game_runtime.participant import ParticipantReference, ParticipantType
from game_runtime.persistence.database import DEFAULT_GAME_DATABASE_PATH, SQLiteGameDatabase
from game_runtime.persistence.repositories.session import GameSessionRepository
from game_runtime.session import GamePhase, GameSession, GameSessionStatus


class AdminGameService:
    def __init__(self, database_path: str | Path = DEFAULT_GAME_DATABASE_PATH) -> None:
        self.database = SQLiteGameDatabase(database_path)
        self.sessions = GameSessionRepository(self.database)

    async def create_session(self, *, group_id: str, dm_qq_id: str) -> dict[str, object]:
        if not group_id.strip() or not dm_qq_id.strip():
            raise ValueError("group_id and dm_qq_id are required")
        await self.database.initialize()
        game_id = f"game-{uuid4()}"
        participant_id = f"dm-{dm_qq_id.strip()}"
        session = GameSession(
            game_id=game_id,
            session_id=f"session-{uuid4()}",
            group_id=group_id.strip(),
            dm_identity=DMIdentity(participant_id=participant_id, qq_id=dm_qq_id.strip()),
            participant_references=(ParticipantReference(participant_id, dm_qq_id.strip(), ParticipantType.DM),),
        )
        await self.sessions.create_session(session)
        return self.serialize(session)

    async def list_sessions(self) -> list[dict[str, object]]:
        await self.database.initialize()
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT game_id FROM game_sessions WHERE status != 'ENDED' "
                "ORDER BY updated_at DESC, game_id"
            )
            game_ids = [str(row["game_id"]) for row in await cursor.fetchall()]
        sessions = []
        for game_id in game_ids:
            session = await self.sessions.get_session(game_id)
            if session is not None:
                sessions.append(self.serialize(session))
        return sessions

    async def control(self, game_id: str, action: str, *, phase: str | None = None) -> dict[str, object]:
        await self.database.initialize()
        session = await self.sessions.get_session(game_id)
        if session is None:
            raise ValueError("game session not found")
        expected = session.state_version
        action = action.strip().lower()
        if action == "start" or action == "resume":
            session.transition_to(GameSessionStatus.RUNNING)
        elif action == "pause":
            session.transition_to(GameSessionStatus.PAUSED)
        elif action == "end":
            session.transition_to(GameSessionStatus.ENDED)
        elif action == "phase" and phase:
            session.transition_phase_to(GamePhase(phase.upper()))
        else:
            raise ValueError("unsupported action")
        await self.sessions.update_session(session, expected_state_version=expected)
        return self.serialize(session)

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
