from datetime import datetime, timezone

import pytest

from game_runtime.event import GameEvent, GameEventSource, GameEventType


def make_event(event_id: str, game_id: str = "game-1") -> GameEvent:
    return GameEvent(
        event_id=event_id,
        game_id=game_id,
        session_id=f"session-{game_id}",
        event_type=GameEventType.MESSAGE_RECEIVED,
        actor="participant-player",
        source=GameEventSource.PLATFORM,
        timestamp=datetime(2026, 7, 14, tzinfo=timezone.utc),
        payload={"classification": "unclassified"},
        correlation_id=f"correlation-{event_id}",
    )


def test_event_is_created_with_required_game_binding() -> None:
    event = make_event("event-1")

    assert event.event_id == "event-1"
    assert event.game_id == "game-1"
    assert event.event_type is GameEventType.MESSAGE_RECEIVED


def test_event_rejects_empty_game_id() -> None:
    with pytest.raises(ValueError, match="game_id"):
        make_event("event-1", game_id="")


def test_events_keep_different_game_ids_isolated() -> None:
    first = make_event("event-1", game_id="game-1")
    second = make_event("event-2", game_id="game-2")

    assert first.game_id != second.game_id
