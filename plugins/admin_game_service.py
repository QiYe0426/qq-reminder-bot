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

    async def create_session(
        self,
        *,
        group_id: str,
        dm_qq_id: str,
        script_id: str,
        players: list[dict[str, object]],
    ) -> dict[str, object]:
        if not group_id.strip() or not dm_qq_id.strip():
            raise ValueError("group_id and dm_qq_id are required")
        if not script_id.strip():
            raise ValueError("script_id is required")
        if not players:
            raise ValueError("at least one player with a character assignment is required")
        await self.database.initialize()
        await self._ensure_admin_setup_table()
        game_id = f"game-{uuid4()}"
        participant_id = f"dm-{dm_qq_id.strip()}"
        participant_references = [
            ParticipantReference(participant_id, dm_qq_id.strip(), ParticipantType.DM)
        ]
        seen_qq_ids = {dm_qq_id.strip()}
        for index, item in enumerate(players, start=1):
            qq_id = str(item.get("qq_id") or "").strip()
            character_id = str(item.get("character_id") or "").strip()
            if not qq_id or not character_id:
                raise ValueError("every player requires qq_id and character_id")
            if qq_id in seen_qq_ids:
                raise ValueError("player qq_id values must be unique")
            seen_qq_ids.add(qq_id)
            participant_references.append(
                ParticipantReference(
                    participant_id=f"player-{index}-{qq_id}",
                    qq_id=qq_id,
                    participant_type=ParticipantType.PLAYER,
                    character_id=character_id,
                )
            )
        session = GameSession(
            game_id=game_id,
            session_id=f"session-{uuid4()}",
            group_id=group_id.strip(),
            dm_identity=DMIdentity(participant_id=participant_id, qq_id=dm_qq_id.strip()),
            participant_references=tuple(participant_references),
        )
        await self.sessions.create_session(session)
        async with self.database.connection() as connection:
            await connection.execute(
                "INSERT INTO game_admin_setup(game_id, script_id) VALUES (?, ?)",
                (game_id, script_id.strip()),
            )
            await connection.commit()
        return self.serialize(session, script_id=script_id.strip())

    async def list_sessions(self) -> list[dict[str, object]]:
        await self.database.initialize()
        await self._ensure_admin_setup_table()
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
                sessions.append(self.serialize(session, script_id=await self._script_id(game_id)))
        return sessions

    async def control(self, game_id: str, action: str, *, phase: str | None = None) -> dict[str, object]:
        await self.database.initialize()
        await self._ensure_admin_setup_table()
        session = await self.sessions.get_session(game_id)
        if session is None:
            raise ValueError("game session not found")
        expected = session.state_version
        action = action.strip().lower()
        script_id = await self._script_id(game_id)
        if action == "start" and not script_id:
            raise ValueError("game setup is incomplete")
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
        return self.serialize(session, script_id=script_id)

    async def _ensure_admin_setup_table(self) -> None:
        async with self.database.connection() as connection:
            await connection.execute(
                "CREATE TABLE IF NOT EXISTS game_admin_setup ("
                "game_id TEXT PRIMARY KEY, script_id TEXT NOT NULL, "
                "FOREIGN KEY(game_id) REFERENCES game_sessions(game_id))"
            )
            await connection.commit()

    async def _script_id(self, game_id: str) -> str | None:
        async with self.database.connection() as connection:
            cursor = await connection.execute(
                "SELECT script_id FROM game_admin_setup WHERE game_id = ?", (game_id,)
            )
            row = await cursor.fetchone()
        return str(row["script_id"]) if row is not None else None

    @staticmethod
    def serialize(session: GameSession, *, script_id: str | None = None) -> dict[str, object]:
        return {
            "game_id": session.game_id,
            "session_id": session.session_id,
            "group_id": session.group_id,
            "dm_qq_id": session.dm_identity.qq_id,
            "status": session.status.value,
            "phase": session.current_phase.value,
            "state_version": session.state_version,
            "participants": len(session.participant_references),
            "script_id": script_id,
            "updated_at": session.updated_at.isoformat(),
        }
