"""Append-only structured GameEvent repository."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3

from game_runtime.errors import DuplicateEventError, PersistenceError
from game_runtime.event import GameEvent, GameEventSource, GameEventType
from game_runtime.persistence.database import SQLiteGameDatabase
from game_runtime.persistence.records import EventProcessingStatus, StoredGameEvent


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class GameEventRepository:
    """Appends immutable Event envelopes and updates a separate processing view."""

    def __init__(self, database: SQLiteGameDatabase) -> None:
        self._database = database

    async def append_event(self, event: GameEvent) -> StoredGameEvent:
        payload_json = self._serialize_payload(event.payload)
        try:
            async with self._database.connection() as connection:
                await connection.execute("BEGIN IMMEDIATE")
                try:
                    existing = await self._load_by_event_id(connection, event.event_id)
                    if existing is not None:
                        if existing.event != event:
                            raise DuplicateEventError(
                                "event_id already belongs to a different Event"
                            )
                        await connection.rollback()
                        return existing
                    cursor = await connection.execute(
                        """
                        SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next_sequence
                        FROM game_events WHERE game_id = ?
                        """,
                        (event.game_id,),
                    )
                    row = await cursor.fetchone()
                    sequence_no = int(row["next_sequence"])
                    await connection.execute(
                        """
                        INSERT INTO game_events(
                            event_id, game_id, session_id, sequence_no,
                            event_type, source, actor, timestamp,
                            payload_json, correlation_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event.event_id,
                            event.game_id,
                            event.session_id,
                            sequence_no,
                            event.event_type.value,
                            event.source.value,
                            event.actor,
                            event.timestamp.isoformat(),
                            payload_json,
                            event.correlation_id,
                        ),
                    )
                    await connection.execute(
                        """
                        INSERT INTO game_event_processing(
                            event_id, game_id, status, updated_at
                        ) VALUES (?, ?, 'RECEIVED', ?)
                        """,
                        (event.event_id, event.game_id, _utc_iso()),
                    )
                except Exception:
                    await connection.rollback()
                    raise
                else:
                    await connection.commit()
        except DuplicateEventError:
            raise
        except sqlite3.IntegrityError as exc:
            raise PersistenceError("event scope does not match a persisted session") from exc
        except sqlite3.Error as exc:
            raise PersistenceError("failed to append game event") from exc
        return StoredGameEvent(
            event=event,
            sequence_no=sequence_no,
            processing_status=EventProcessingStatus.RECEIVED,
        )

    async def get_events(self, game_id: str) -> tuple[StoredGameEvent, ...]:
        if not game_id:
            raise ValueError("game_id must not be empty")
        try:
            async with self._database.connection() as connection:
                cursor = await connection.execute(
                    """
                    SELECT e.*, p.status AS processing_status,
                           p.applied_state_version, p.error_code
                    FROM game_events AS e
                    JOIN game_event_processing AS p ON p.event_id = e.event_id
                    WHERE e.game_id = ?
                    ORDER BY e.sequence_no
                    """,
                    (game_id,),
                )
                rows = await cursor.fetchall()
        except sqlite3.Error as exc:
            raise PersistenceError("failed to load game events") from exc
        return tuple(self._to_stored_event(row) for row in rows)

    async def mark_processed(
        self,
        game_id: str,
        event_id: str,
        status: EventProcessingStatus,
        *,
        applied_state_version: int | None = None,
        error_code: str | None = None,
    ) -> None:
        if status in {
            EventProcessingStatus.RECEIVED,
            EventProcessingStatus.APPLIED,
        }:
            raise ValueError(
                "mark_processed only supports REJECTED/DEFERRED; "
                "APPLIED must use the atomic State apply boundary"
            )
        try:
            async with self._database.connection() as connection:
                cursor = await connection.execute(
                    """
                    UPDATE game_event_processing SET
                        status = ?, applied_state_version = ?,
                        error_code = ?, updated_at = ?
                    WHERE game_id = ? AND event_id = ? AND status = 'RECEIVED'
                    """,
                    (
                        status.value,
                        applied_state_version,
                        error_code,
                        _utc_iso(),
                        game_id,
                        event_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise PersistenceError(
                        "event is missing or has already left RECEIVED"
                    )
                await connection.commit()
        except PersistenceError:
            raise
        except sqlite3.Error as exc:
            raise PersistenceError("failed to update event processing state") from exc

    async def _load_by_event_id(self, connection, event_id: str) -> StoredGameEvent | None:
        cursor = await connection.execute(
            """
            SELECT e.*, p.status AS processing_status,
                   p.applied_state_version, p.error_code
            FROM game_events AS e
            JOIN game_event_processing AS p ON p.event_id = e.event_id
            WHERE e.event_id = ?
            """,
            (event_id,),
        )
        row = await cursor.fetchone()
        return self._to_stored_event(row) if row is not None else None

    @staticmethod
    def _serialize_payload(payload) -> str:
        try:
            return json.dumps(
                dict(payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("event payload must be JSON serializable") from exc

    @staticmethod
    def _to_stored_event(row) -> StoredGameEvent:
        timestamp = datetime.fromisoformat(str(row["timestamp"]))
        return StoredGameEvent(
            event=GameEvent(
                event_id=str(row["event_id"]),
                game_id=str(row["game_id"]),
                session_id=str(row["session_id"]),
                event_type=GameEventType(str(row["event_type"])),
                actor=str(row["actor"]),
                source=GameEventSource(str(row["source"])),
                timestamp=timestamp,
                payload=json.loads(str(row["payload_json"])),
                correlation_id=str(row["correlation_id"]),
            ),
            sequence_no=int(row["sequence_no"]),
            processing_status=EventProcessingStatus(str(row["processing_status"])),
            applied_state_version=(
                int(row["applied_state_version"])
                if row["applied_state_version"] is not None
                else None
            ),
            error_code=(
                str(row["error_code"]) if row["error_code"] is not None else None
            ),
        )
