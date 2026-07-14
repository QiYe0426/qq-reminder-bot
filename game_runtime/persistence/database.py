"""SQLite connection and minimal schema migration management."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from game_runtime.errors import PersistenceError, UnsupportedSchemaVersion
from game_runtime.persistence.schema import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    SCHEMA_VERSION_SQL,
)

DEFAULT_GAME_DATABASE_PATH = Path("data/game_runtime/game.db")


class SQLiteGameDatabase:
    """Owns connections to the dedicated Game Runtime database."""

    def __init__(self, path: str | Path = DEFAULT_GAME_DATABASE_PATH) -> None:
        self.path = Path(path)

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiosqlite.connect(self.path) as connection:
                await connection.execute("PRAGMA foreign_keys = ON")
                await connection.execute("PRAGMA journal_mode = WAL")
                await connection.execute("PRAGMA busy_timeout = 5000")
                await connection.execute(SCHEMA_VERSION_SQL)
                await connection.execute(
                    """
                    INSERT OR IGNORE INTO game_schema_version(singleton_id, version)
                    VALUES (1, 0)
                    """
                )
                cursor = await connection.execute(
                    "SELECT version FROM game_schema_version WHERE singleton_id = 1"
                )
                row = await cursor.fetchone()
                current_version = int(row[0])
                if current_version > CURRENT_SCHEMA_VERSION:
                    raise UnsupportedSchemaVersion(
                        "game database schema is newer than this Runtime: "
                        f"{current_version} > {CURRENT_SCHEMA_VERSION}"
                    )
                await connection.commit()
                for version in range(current_version + 1, CURRENT_SCHEMA_VERSION + 1):
                    await connection.execute("BEGIN IMMEDIATE")
                    try:
                        for statement in MIGRATIONS[version]:
                            await connection.execute(statement)
                        await connection.execute(
                            """
                            UPDATE game_schema_version SET version = ?
                            WHERE singleton_id = 1
                            """,
                            (version,),
                        )
                    except Exception:
                        await connection.rollback()
                        raise
                    else:
                        await connection.commit()
                await connection.commit()
        except UnsupportedSchemaVersion:
            raise
        except aiosqlite.Error as exc:
            raise PersistenceError("failed to initialize game database") from exc

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        connection: aiosqlite.Connection | None = None
        try:
            connection = await aiosqlite.connect(self.path)
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA foreign_keys = ON")
            await connection.execute("PRAGMA busy_timeout = 5000")
        except aiosqlite.Error as exc:
            if connection is not None:
                await connection.close()
            raise PersistenceError("failed to open game database") from exc
        try:
            yield connection
        finally:
            await connection.close()

    async def schema_version(self) -> int:
        try:
            async with self.connection() as connection:
                cursor = await connection.execute(
                    "SELECT version FROM game_schema_version WHERE singleton_id = 1"
                )
                row = await cursor.fetchone()
        except aiosqlite.Error as exc:
            raise PersistenceError("failed to read game schema version") from exc
        if row is None:
            raise PersistenceError("game schema version is missing")
        return int(row["version"])
