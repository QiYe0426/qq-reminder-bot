"""Process clean-shutdown marker for recovery policy selection."""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

from game_runtime.errors import PersistenceError
from game_runtime.persistence.database import SQLiteGameDatabase


class RuntimeStateRepository:
    def __init__(self, database: SQLiteGameDatabase) -> None:
        self._database = database

    async def begin_runtime(self) -> bool:
        """Return whether the previous shutdown was clean, then mark this run dirty."""

        now = datetime.now(timezone.utc).isoformat()
        try:
            async with self._database.connection() as connection:
                await connection.execute("BEGIN IMMEDIATE")
                cursor = await connection.execute(
                    """
                    SELECT clean_shutdown FROM game_runtime_state
                    WHERE singleton_id = 1
                    """
                )
                row = await cursor.fetchone()
                was_clean = bool(row["clean_shutdown"]) if row is not None else False
                await connection.execute(
                    """
                    INSERT INTO game_runtime_state(singleton_id, clean_shutdown, updated_at)
                    VALUES (1, 0, ?)
                    ON CONFLICT(singleton_id) DO UPDATE SET
                        clean_shutdown = 0, updated_at = excluded.updated_at
                    """,
                    (now,),
                )
                await connection.commit()
                return was_clean
        except sqlite3.Error as exc:
            raise PersistenceError("failed to begin Game Runtime recovery") from exc

    async def mark_clean_shutdown(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        try:
            async with self._database.connection() as connection:
                cursor = await connection.execute(
                    """
                    UPDATE game_runtime_state
                    SET clean_shutdown = 1, updated_at = ?
                    WHERE singleton_id = 1
                    """,
                    (now,),
                )
                if cursor.rowcount != 1:
                    raise PersistenceError("runtime state has not been initialized")
                await connection.commit()
        except PersistenceError:
            raise
        except sqlite3.Error as exc:
            raise PersistenceError("failed to mark clean Game Runtime shutdown") from exc
