"""Versioned SQLite schema for the isolated Game Runtime database."""

CURRENT_SCHEMA_VERSION = 1

SCHEMA_VERSION_SQL = """
CREATE TABLE IF NOT EXISTS game_schema_version (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    version INTEGER NOT NULL CHECK (version >= 0)
)
"""

MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE game_sessions (
            game_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL UNIQUE,
            group_id TEXT NOT NULL,
            dm_participant_id TEXT NOT NULL,
            dm_qq_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('CREATED', 'RUNNING', 'PAUSED', 'ENDED')
            ),
            phase TEXT NOT NULL CHECK (
                phase IN (
                    'LOBBY', 'INTRODUCTION', 'EXPLORATION',
                    'DISCUSSION', 'VOTING', 'ENDING'
                )
            ),
            state_version INTEGER NOT NULL CHECK (state_version >= 0),
            last_applied_sequence_no INTEGER NOT NULL DEFAULT 0
                CHECK (last_applied_sequence_no >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (game_id, session_id)
        )
        """,
        """
        CREATE TABLE game_participants (
            game_id TEXT NOT NULL,
            participant_id TEXT NOT NULL,
            qq_id TEXT NOT NULL,
            participant_type TEXT NOT NULL CHECK (
                participant_type IN ('DM', 'PLAYER', 'SPECTATOR', 'UNKNOWN')
            ),
            character_id TEXT,
            membership_state TEXT NOT NULL CHECK (
                membership_state IN ('ACTIVE', 'REPLACED', 'LEFT', 'REVOKED')
            ),
            PRIMARY KEY (game_id, participant_id),
            FOREIGN KEY (game_id) REFERENCES game_sessions(game_id)
                ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE game_ownership_generations (
            group_id TEXT PRIMARY KEY,
            last_generation INTEGER NOT NULL CHECK (last_generation > 0)
        )
        """,
        """
        CREATE TABLE game_active_ownership (
            group_id TEXT PRIMARY KEY,
            game_id TEXT NOT NULL UNIQUE,
            session_id TEXT NOT NULL UNIQUE,
            session_status TEXT NOT NULL CHECK (
                session_status IN ('RUNNING', 'PAUSED')
            ),
            ownership_generation INTEGER NOT NULL
                CHECK (ownership_generation > 0),
            state_version INTEGER NOT NULL CHECK (state_version >= 0),
            updated_at TEXT NOT NULL,
            FOREIGN KEY (game_id, session_id)
                REFERENCES game_sessions(game_id, session_id)
                ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE game_events (
            event_id TEXT PRIMARY KEY,
            game_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            sequence_no INTEGER NOT NULL CHECK (sequence_no > 0),
            event_type TEXT NOT NULL,
            source TEXT NOT NULL,
            actor TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            UNIQUE (game_id, sequence_no),
            FOREIGN KEY (game_id, session_id)
                REFERENCES game_sessions(game_id, session_id)
                ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE game_event_processing (
            event_id TEXT PRIMARY KEY,
            game_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('RECEIVED', 'APPLIED', 'REJECTED', 'DEFERRED')
            ),
            applied_state_version INTEGER,
            error_code TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (event_id) REFERENCES game_events(event_id)
                ON DELETE CASCADE,
            FOREIGN KEY (game_id) REFERENCES game_sessions(game_id)
                ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE game_actions (
            action_id TEXT PRIMARY KEY,
            game_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            action_type TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN (
                    'CREATED', 'EXECUTING', 'SUCCESS',
                    'FAILED', 'UNKNOWN', 'CANCELLED'
                )
            ),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (game_id, session_id)
                REFERENCES game_sessions(game_id, session_id)
                ON DELETE CASCADE
        )
        """,
        """
        CREATE INDEX idx_game_events_game_sequence
            ON game_events(game_id, sequence_no)
        """,
        """
        CREATE INDEX idx_game_actions_game_status
            ON game_actions(game_id, status)
        """,
        """
        CREATE TABLE game_runtime_state (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            clean_shutdown INTEGER NOT NULL CHECK (clean_shutdown IN (0, 1)),
            updated_at TEXT NOT NULL
        )
        """,
    )
}
