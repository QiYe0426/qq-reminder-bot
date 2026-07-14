"""Persistent Game Action state repository without an Executor."""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

from game_runtime.action import GameAction, GameActionStatus
from game_runtime.errors import PersistenceConflict, PersistenceError
from game_runtime.persistence.database import SQLiteGameDatabase


class GameActionRepository:
    def __init__(self, database: SQLiteGameDatabase) -> None:
        self._database = database

    async def create_action(self, action: GameAction) -> None:
        try:
            async with self._database.connection() as connection:
                await connection.execute(
                    """
                    INSERT INTO game_actions(
                        action_id, game_id, session_id, action_type,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        action.action_id,
                        action.game_id,
                        action.session_id,
                        action.action_type,
                        action.status.value,
                        action.created_at.isoformat(),
                        action.updated_at.isoformat(),
                    ),
                )
                await connection.commit()
        except sqlite3.IntegrityError as exc:
            raise PersistenceConflict("action already exists or has invalid scope") from exc
        except sqlite3.Error as exc:
            raise PersistenceError("failed to create game action") from exc

    async def get_action(self, game_id: str, action_id: str) -> GameAction | None:
        if not game_id:
            raise ValueError("game_id must not be empty")
        try:
            async with self._database.connection() as connection:
                cursor = await connection.execute(
                    """
                    SELECT * FROM game_actions
                    WHERE game_id = ? AND action_id = ?
                    """,
                    (game_id, action_id),
                )
                row = await cursor.fetchone()
        except sqlite3.Error as exc:
            raise PersistenceError("failed to load game action") from exc
        return self._to_action(row) if row is not None else None

    async def list_actions(self, game_id: str) -> tuple[GameAction, ...]:
        if not game_id:
            raise ValueError("game_id must not be empty")
        try:
            async with self._database.connection() as connection:
                cursor = await connection.execute(
                    """
                    SELECT * FROM game_actions
                    WHERE game_id = ? ORDER BY created_at, action_id
                    """,
                    (game_id,),
                )
                rows = await cursor.fetchall()
        except sqlite3.Error as exc:
            raise PersistenceError("failed to list game actions") from exc
        return tuple(self._to_action(row) for row in rows)

    async def transition_action(
        self,
        game_id: str,
        action_id: str,
        *,
        expected_status: GameActionStatus,
        target_status: GameActionStatus,
    ) -> GameAction:
        action = await self.get_action(game_id, action_id)
        if action is None:
            raise PersistenceError("action does not exist in this game")
        if action.status is not expected_status:
            raise PersistenceConflict("action status changed before transition")
        action.transition_to(target_status)
        try:
            async with self._database.connection() as connection:
                cursor = await connection.execute(
                    """
                    UPDATE game_actions SET status = ?, updated_at = ?
                    WHERE game_id = ? AND action_id = ? AND status = ?
                    """,
                    (
                        action.status.value,
                        action.updated_at.isoformat(),
                        game_id,
                        action_id,
                        expected_status.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise PersistenceConflict("action status changed during transition")
                await connection.commit()
        except PersistenceConflict:
            raise
        except sqlite3.Error as exc:
            raise PersistenceError("failed to transition game action") from exc
        return action

    async def recover_executing_as_unknown(self, game_id: str) -> int:
        if not game_id:
            raise ValueError("game_id must not be empty")
        updated_at = datetime.now(timezone.utc).isoformat()
        try:
            async with self._database.connection() as connection:
                cursor = await connection.execute(
                    """
                    UPDATE game_actions SET status = 'UNKNOWN', updated_at = ?
                    WHERE game_id = ? AND status = 'EXECUTING'
                    """,
                    (updated_at, game_id),
                )
                await connection.commit()
                return max(cursor.rowcount, 0)
        except sqlite3.Error as exc:
            raise PersistenceError("failed to recover executing game actions") from exc

    @staticmethod
    def _to_action(row) -> GameAction:
        return GameAction(
            action_id=str(row["action_id"]),
            game_id=str(row["game_id"]),
            session_id=str(row["session_id"]),
            action_type=str(row["action_type"]),
            status=GameActionStatus(str(row["status"])),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )
