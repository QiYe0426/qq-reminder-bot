from datetime import datetime, timezone

import pytest

from game_runtime.actor import GameSessionActor
from game_runtime.errors import EventSessionMismatch
from game_runtime.event import GameEvent, GameEventSource, GameEventType


def make_event(event_id: str, game_id: str = "game-1") -> GameEvent:
    return GameEvent(
        event_id=event_id,
        game_id=game_id,
        session_id=f"session-{game_id}",
        event_type=GameEventType.MESSAGE_RECEIVED,
        actor="participant-player",
        source=GameEventSource.PLATFORM,
        correlation_id=f"correlation-{event_id}",
        timestamp=datetime(2026, 7, 14, tzinfo=timezone.utc),
    )


def test_actor_processes_events_in_receive_order(session_factory) -> None:
    observed: list[str] = []
    actor = GameSessionActor(
        session_factory(),
        state_updater=lambda _session, event: observed.append(event.event_id),
    )

    for event_id in ("event-a", "event-b", "event-c"):
        assert actor.receive(make_event(event_id))

    assert observed == ["event-a", "event-b", "event-c"]
    assert actor.processed_event_ids == ("event-a", "event-b", "event-c")
    assert actor.pending_count == 0


def test_actor_rejects_cross_session_event(session_factory) -> None:
    actor = GameSessionActor(session_factory("game-1"))

    with pytest.raises(EventSessionMismatch):
        actor.receive(make_event("event-x", game_id="game-2"))


def test_actor_deduplicates_event_id_in_memory(session_factory) -> None:
    actor = GameSessionActor(session_factory())
    event = make_event("event-a")

    assert actor.receive(event)
    assert not actor.receive(event)
    assert actor.processed_event_ids == ("event-a",)
