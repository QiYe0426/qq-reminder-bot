"""GameSession and persistent group ownership repositories."""

from __future__ import annotations

from datetime import datetime
import sqlite3

from game_runtime.errors import PersistenceConflict, PersistenceError
from game_runtime.identity import DMIdentity
from game_runtime.participant import (
    ParticipantMembershipState,
    ParticipantReference,
    ParticipantType,
)
from game_runtime.persistence.database import SQLiteGameDatabase
from game_runtime.persistence.records import PersistentOwnership
from game_runtime.routing.models import SessionLookupStatus
from game_runtime.session import GamePhase, GameSession, GameSessionStatus


def _serialize_datetime(value: datetime) -> str:
    return value.isoformat()


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PersistenceError("persisted datetime must be timezone-aware")
    return parsed


class GameSessionRepository:
    """Persists Session snapshots without exposing SQLite to the domain."""

    def __init__(self, database: SQLiteGameDatabase) -> None:
        self._database = database

    async def create_session(self, session: GameSession) -> None:
        if session.status is not GameSessionStatus.CREATED:
            raise ValueError("new persisted session must start in CREATED")
        try:
            async with self._database.connection() as connection:
                await connection.execute("BEGIN IMMEDIATE")
                try:
                    await connection.execute(
                        """
                        INSERT INTO game_sessions(
                            game_id, session_id, group_id,
                            dm_participant_id, dm_qq_id,
                            status, phase, state_version,
                            last_applied_sequence_no, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        self._session_values(session),
                    )
                    await self._replace_participants(connection, session)
                    await self._sync_ownership(connection, session)
                except Exception:
                    await connection.rollback()
                    raise
                else:
                    await connection.commit()
        except sqlite3.IntegrityError as exc:
            raise PersistenceConflict("session or ownership already exists") from exc
        except PersistenceConflict:
            raise
        except sqlite3.Error as exc:
            raise PersistenceError("failed to create game session") from exc

    async def get_session(self, game_id: str) -> GameSession | None:
        if not game_id:
            raise ValueError("game_id must not be empty")
        try:
            async with self._database.connection() as connection:
                cursor = await connection.execute(
                    "SELECT * FROM game_sessions WHERE game_id = ?",
                    (game_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    return None
                participants = await self._load_participants(connection, game_id)
        except sqlite3.Error as exc:
            raise PersistenceError("failed to load game session") from exc
        return self._to_session(row, participants)

    async def list_active_sessions(self) -> tuple[GameSession, ...]:
        try:
            async with self._database.connection() as connection:
                cursor = await connection.execute(
                    """
                    SELECT game_id FROM game_sessions
                    WHERE status IN ('RUNNING', 'PAUSED')
                    ORDER BY created_at, game_id
                    """
                )
                game_ids = [str(row["game_id"]) for row in await cursor.fetchall()]
        except sqlite3.Error as exc:
            raise PersistenceError("failed to list active game sessions") from exc

        sessions: list[GameSession] = []
        for game_id in game_ids:
            session = await self.get_session(game_id)
            if session is None:
                raise PersistenceError("active session disappeared during recovery")
            sessions.append(session)
        return tuple(sessions)

    async def update_session(
        self,
        session: GameSession,
        *,
        expected_state_version: int,
    ) -> None:
        if expected_state_version < 0:
            raise ValueError("expected_state_version must not be negative")
        if session.state_version != expected_state_version + 1:
            raise ValueError("session update must advance state_version exactly once")
        try:
            async with self._database.connection() as connection:
                await connection.execute("BEGIN IMMEDIATE")
                try:
                    cursor = await connection.execute(
                        """
                        UPDATE game_sessions SET
                            group_id = ?, dm_participant_id = ?, dm_qq_id = ?,
                            status = ?, phase = ?, state_version = ?,
                            last_applied_sequence_no = ?, updated_at = ?
                        WHERE game_id = ? AND session_id = ? AND state_version = ?
                        """,
                        (
                            session.group_id,
                            session.dm_identity.participant_id,
                            session.dm_identity.qq_id,
                            session.status.value,
                            session.current_phase.value,
                            session.state_version,
                            session.last_applied_sequence_no,
                            _serialize_datetime(session.updated_at),
                            session.game_id,
                            session.session_id,
                            expected_state_version,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise PersistenceConflict(
                            "session state_version changed or session is missing"
                        )
                    await self._replace_participants(connection, session)
                    await self._sync_ownership(connection, session)
                except Exception:
                    await connection.rollback()
                    raise
                else:
                    await connection.commit()
        except sqlite3.IntegrityError as exc:
            raise PersistenceConflict("session ownership conflicts with another game") from exc
        except PersistenceConflict:
            raise
        except sqlite3.Error as exc:
            raise PersistenceError("failed to update game session") from exc

    async def apply_event(
        self,
        session: GameSession,
        *,
        event_id: str,
        expected_state_version: int,
    ) -> None:
        """Atomically persist a State delta and mark its next Event APPLIED."""

        if session.state_version != expected_state_version + 1:
            raise ValueError("applied event must advance state_version exactly once")
        try:
            async with self._database.connection() as connection:
                await connection.execute("BEGIN IMMEDIATE")
                try:
                    cursor = await connection.execute(
                        """
                        SELECT e.sequence_no, p.status
                        FROM game_events AS e
                        JOIN game_event_processing AS p ON p.event_id = e.event_id
                        WHERE e.game_id = ? AND e.session_id = ? AND e.event_id = ?
                        """,
                        (session.game_id, session.session_id, event_id),
                    )
                    event_row = await cursor.fetchone()
                    if event_row is None or str(event_row["status"]) != "RECEIVED":
                        raise PersistenceConflict(
                            "event is missing or has already been processed"
                        )
                    sequence_no = int(event_row["sequence_no"])
                    if session.last_applied_sequence_no != sequence_no:
                        raise PersistenceConflict(
                            "session cursor does not point to the applied Event"
                        )

                    cursor = await connection.execute(
                        """
                        UPDATE game_sessions SET
                            group_id = ?, dm_participant_id = ?, dm_qq_id = ?,
                            status = ?, phase = ?, state_version = ?,
                            last_applied_sequence_no = ?, updated_at = ?
                        WHERE game_id = ? AND session_id = ?
                          AND state_version = ?
                          AND last_applied_sequence_no = ?
                        """,
                        (
                            session.group_id,
                            session.dm_identity.participant_id,
                            session.dm_identity.qq_id,
                            session.status.value,
                            session.current_phase.value,
                            session.state_version,
                            session.last_applied_sequence_no,
                            _serialize_datetime(session.updated_at),
                            session.game_id,
                            session.session_id,
                            expected_state_version,
                            sequence_no - 1,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise PersistenceConflict(
                            "session version or Event cursor changed during apply"
                        )
                    cursor = await connection.execute(
                        """
                        UPDATE game_event_processing SET
                            status = 'APPLIED', applied_state_version = ?,
                            error_code = NULL, updated_at = ?
                        WHERE event_id = ? AND game_id = ? AND status = 'RECEIVED'
                        """,
                        (
                            session.state_version,
                            _serialize_datetime(session.updated_at),
                            event_id,
                            session.game_id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise PersistenceConflict(
                            "event processing state changed during apply"
                        )
                    await self._replace_participants(connection, session)
                    await self._sync_ownership(connection, session)
                except Exception:
                    await connection.rollback()
                    raise
                else:
                    await connection.commit()
        except sqlite3.IntegrityError as exc:
            raise PersistenceConflict("event apply violates persistence scope") from exc
        except PersistenceConflict:
            raise
        except sqlite3.Error as exc:
            raise PersistenceError("failed to atomically apply game event") from exc

    async def get_ownership(self, group_id: str) -> PersistentOwnership | None:
        if not group_id:
            raise ValueError("group_id must not be empty")
        try:
            async with self._database.connection() as connection:
                cursor = await connection.execute(
                    "SELECT * FROM game_active_ownership WHERE group_id = ?",
                    (group_id,),
                )
                row = await cursor.fetchone()
        except sqlite3.Error as exc:
            raise PersistenceError("failed to load game ownership") from exc
        if row is None:
            return None
        return PersistentOwnership(
            group_id=str(row["group_id"]),
            game_id=str(row["game_id"]),
            session_id=str(row["session_id"]),
            session_status=SessionLookupStatus(str(row["session_status"])),
            ownership_generation=int(row["ownership_generation"]),
            state_version=int(row["state_version"]),
            updated_at=_parse_datetime(str(row["updated_at"])),
        )

    @staticmethod
    def _session_values(session: GameSession) -> tuple[object, ...]:
        return (
            session.game_id,
            session.session_id,
            session.group_id,
            session.dm_identity.participant_id,
            session.dm_identity.qq_id,
            session.status.value,
            session.current_phase.value,
            session.state_version,
            session.last_applied_sequence_no,
            _serialize_datetime(session.created_at),
            _serialize_datetime(session.updated_at),
        )

    @staticmethod
    async def _replace_participants(connection, session: GameSession) -> None:
        await connection.execute(
            "DELETE FROM game_participants WHERE game_id = ?",
            (session.game_id,),
        )
        await connection.executemany(
            """
            INSERT INTO game_participants(
                game_id, participant_id, qq_id, participant_type,
                character_id, membership_state
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    session.game_id,
                    participant.participant_id,
                    participant.qq_id,
                    participant.participant_type.value,
                    participant.character_id,
                    participant.membership_state.value,
                )
                for participant in session.participant_references
            ],
        )

    @staticmethod
    async def _load_participants(connection, game_id: str) -> tuple[ParticipantReference, ...]:
        cursor = await connection.execute(
            """
            SELECT participant_id, qq_id, participant_type,
                   character_id, membership_state
            FROM game_participants
            WHERE game_id = ?
            ORDER BY participant_id
            """,
            (game_id,),
        )
        rows = await cursor.fetchall()
        return tuple(
            ParticipantReference(
                participant_id=str(row["participant_id"]),
                qq_id=str(row["qq_id"]),
                participant_type=ParticipantType(str(row["participant_type"])),
                character_id=(
                    str(row["character_id"])
                    if row["character_id"] is not None
                    else None
                ),
                membership_state=ParticipantMembershipState(
                    str(row["membership_state"])
                ),
            )
            for row in rows
        )

    @staticmethod
    def _to_session(row, participants: tuple[ParticipantReference, ...]) -> GameSession:
        return GameSession(
            game_id=str(row["game_id"]),
            session_id=str(row["session_id"]),
            group_id=str(row["group_id"]),
            dm_identity=DMIdentity(
                participant_id=str(row["dm_participant_id"]),
                qq_id=str(row["dm_qq_id"]),
            ),
            participant_references=participants,
            status=GameSessionStatus(str(row["status"])),
            current_phase=GamePhase(str(row["phase"])),
            state_version=int(row["state_version"]),
            last_applied_sequence_no=int(row["last_applied_sequence_no"]),
            created_at=_parse_datetime(str(row["created_at"])),
            updated_at=_parse_datetime(str(row["updated_at"])),
        )

    @staticmethod
    async def _sync_ownership(connection, session: GameSession) -> None:
        if session.status not in {GameSessionStatus.RUNNING, GameSessionStatus.PAUSED}:
            await connection.execute(
                "DELETE FROM game_active_ownership WHERE game_id = ?",
                (session.game_id,),
            )
            return

        cursor = await connection.execute(
            "SELECT * FROM game_active_ownership WHERE group_id = ?",
            (session.group_id,),
        )
        existing = await cursor.fetchone()
        if existing is not None and str(existing["game_id"]) != session.game_id:
            raise PersistenceConflict("group already belongs to another active game")

        if existing is None:
            cursor = await connection.execute(
                """
                SELECT last_generation FROM game_ownership_generations
                WHERE group_id = ?
                """,
                (session.group_id,),
            )
            generation_row = await cursor.fetchone()
            generation = (
                int(generation_row["last_generation"]) + 1
                if generation_row is not None
                else 1
            )
            await connection.execute(
                """
                INSERT INTO game_ownership_generations(group_id, last_generation)
                VALUES (?, ?)
                ON CONFLICT(group_id) DO UPDATE SET last_generation = excluded.last_generation
                """,
                (session.group_id, generation),
            )
            await connection.execute(
                """
                INSERT INTO game_active_ownership(
                    group_id, game_id, session_id, session_status,
                    ownership_generation, state_version, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.group_id,
                    session.game_id,
                    session.session_id,
                    session.status.value,
                    generation,
                    session.state_version,
                    _serialize_datetime(session.updated_at),
                ),
            )
            return

        await connection.execute(
            """
            UPDATE game_active_ownership SET
                session_status = ?, state_version = ?, updated_at = ?
            WHERE group_id = ? AND game_id = ? AND session_id = ?
            """,
            (
                session.status.value,
                session.state_version,
                _serialize_datetime(session.updated_at),
                session.group_id,
                session.game_id,
                session.session_id,
            ),
        )


class ParticipantRepository:
    """Provides only game-scoped Participant reads and writes."""

    def __init__(self, database: SQLiteGameDatabase) -> None:
        self._database = database

    async def save_participant(
        self,
        game_id: str,
        participant: ParticipantReference,
    ) -> None:
        if not game_id:
            raise ValueError("game_id must not be empty")
        try:
            async with self._database.connection() as connection:
                await connection.execute(
                    """
                    INSERT INTO game_participants(
                        game_id, participant_id, qq_id, participant_type,
                        character_id, membership_state
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(game_id, participant_id) DO UPDATE SET
                        qq_id = excluded.qq_id,
                        participant_type = excluded.participant_type,
                        character_id = excluded.character_id,
                        membership_state = excluded.membership_state
                    """,
                    (
                        game_id,
                        participant.participant_id,
                        participant.qq_id,
                        participant.participant_type.value,
                        participant.character_id,
                        participant.membership_state.value,
                    ),
                )
                await connection.commit()
        except sqlite3.IntegrityError as exc:
            raise PersistenceConflict("participant game_id does not exist") from exc
        except sqlite3.Error as exc:
            raise PersistenceError("failed to save game participant") from exc

    async def get_participants(
        self,
        game_id: str,
    ) -> tuple[ParticipantReference, ...]:
        if not game_id:
            raise ValueError("game_id must not be empty")
        try:
            async with self._database.connection() as connection:
                return await GameSessionRepository._load_participants(connection, game_id)
        except sqlite3.Error as exc:
            raise PersistenceError("failed to load game participants") from exc
