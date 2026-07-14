import asyncio
from datetime import datetime, timezone

import pytest

from game_runtime.errors import DuplicateEventError
from game_runtime.event import GameEvent, GameEventSource, GameEventType
from game_runtime.persistence import (
    EventProcessingStatus,
    GameEventRepository,
    GameSessionRepository,
    SQLiteGameDatabase,
)
from game_runtime.session import GamePhase, GameSessionStatus


def run(coroutine):
    return asyncio.run(coroutine)


def make_event(event_id: str, payload: dict[str, object] | None = None) -> GameEvent:
    return GameEvent(
        event_id=event_id,
        game_id="game-1",
        session_id="session-game-1",
        event_type=GameEventType.MESSAGE_RECEIVED,
        actor="participant-player",
        source=GameEventSource.PLATFORM,
        correlation_id=f"correlation-{event_id}",
        timestamp=datetime(2026, 7, 15, tzinfo=timezone.utc),
        payload=payload or {"content_ref": event_id},
    )


def test_event_append_order_survives_repository_reload(session_factory, tmp_path) -> None:
    async def scenario() -> None:
        database = SQLiteGameDatabase(tmp_path / "game.db")
        await database.initialize()
        await GameSessionRepository(database).create_session(session_factory())
        repository = GameEventRepository(database)

        first = await repository.append_event(make_event("event-1"))
        second = await repository.append_event(make_event("event-2"))
        restored = await GameEventRepository(database).get_events("game-1")

        assert first.sequence_no == 1
        assert second.sequence_no == 2
        assert [item.event.event_id for item in restored] == ["event-1", "event-2"]
        assert [item.sequence_no for item in restored] == [1, 2]

    run(scenario())


def test_event_id_is_idempotent_but_cannot_be_overwritten(
    session_factory,
    tmp_path,
) -> None:
    async def scenario() -> None:
        database = SQLiteGameDatabase(tmp_path / "game.db")
        await database.initialize()
        await GameSessionRepository(database).create_session(session_factory())
        repository = GameEventRepository(database)
        original = make_event("event-1")

        first = await repository.append_event(original)
        duplicate = await repository.append_event(original)
        assert duplicate == first

        with pytest.raises(DuplicateEventError):
            await repository.append_event(
                make_event("event-1", payload={"content_ref": "changed"})
            )

    run(scenario())


def test_event_apply_atomically_advances_snapshot_and_processing(
    session_factory,
    tmp_path,
) -> None:
    async def scenario() -> None:
        database = SQLiteGameDatabase(tmp_path / "game.db")
        await database.initialize()
        sessions = GameSessionRepository(database)
        events = GameEventRepository(database)
        session = session_factory()
        await sessions.create_session(session)
        session.transition_to(GameSessionStatus.RUNNING)
        await sessions.update_session(session, expected_state_version=0)
        stored = await events.append_event(make_event("event-1"))

        session.transition_phase_to(GamePhase.EXPLORATION)
        session.last_applied_sequence_no = stored.sequence_no
        await sessions.apply_event(
            session,
            event_id="event-1",
            expected_state_version=1,
        )

        restored_session = await sessions.get_session("game-1")
        restored_events = await events.get_events("game-1")
        assert restored_session is not None
        assert restored_session.state_version == 2
        assert restored_session.last_applied_sequence_no == 1
        assert restored_events[0].processing_status is EventProcessingStatus.APPLIED
        assert restored_events[0].applied_state_version == 2

    run(scenario())
