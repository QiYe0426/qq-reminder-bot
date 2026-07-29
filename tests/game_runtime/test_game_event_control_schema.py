from dataclasses import replace
from datetime import datetime, timezone

import pytest

from game_runtime.event import (
    CONTROL_RESULT_PAYLOAD_TYPES,
    CONTROL_RESULT_VISIBILITY,
    CharacterAssignedPayload,
    ControlResultPayloadSchemaError,
    EventVisibility,
    GameEvent,
    GameEventSource,
    GameEventType,
    PhaseChangedPayload,
    PlayerReplacedPayload,
    ScriptSetPayload,
    SessionControlRejectedPayload,
    SessionCreatedPayload,
    SessionEndedPayload,
    SessionPausedPayload,
    SessionResumedPayload,
    SessionStartedPayload,
    validate_control_result_event,
)
from game_runtime.session import GamePhase, GameSessionStatus


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
COMMON = {
    "command_id": "command-1",
    "operation_id": "operation-1",
    "input_event_id": "event-command-1",
    "result_code": "APPLIED",
    "result_state_version": 5,
}


def payloads_by_type():
    return {
        GameEventType.SESSION_CREATED: SessionCreatedPayload(
            **COMMON,
            status=GameSessionStatus.CREATED,
            phase=GamePhase.LOBBY,
        ),
        GameEventType.SESSION_STARTED: SessionStartedPayload(
            **COMMON,
            previous_status=GameSessionStatus.CREATED,
            current_status=GameSessionStatus.RUNNING,
            ownership_generation=1,
        ),
        GameEventType.SESSION_PAUSED: SessionPausedPayload(
            **COMMON,
            previous_status=GameSessionStatus.RUNNING,
            current_status=GameSessionStatus.PAUSED,
            reason_code="DM_REQUEST",
        ),
        GameEventType.SESSION_RESUMED: SessionResumedPayload(
            **COMMON,
            previous_status=GameSessionStatus.PAUSED,
            current_status=GameSessionStatus.RUNNING,
            ownership_generation=1,
        ),
        GameEventType.SESSION_ENDED: SessionEndedPayload(
            **COMMON,
            previous_status=GameSessionStatus.RUNNING,
            current_status=GameSessionStatus.ENDED,
            retention_reference="retention-1",
        ),
        GameEventType.SCRIPT_SET: ScriptSetPayload(
            **COMMON,
            script_id="script-1",
            public_name="Public Script",
            manifest_reference="manifest-1",
        ),
        GameEventType.CHARACTER_ASSIGNED: CharacterAssignedPayload(
            **COMMON,
            participant_id="participant-1",
            character_id="character-1",
            binding_version=2,
        ),
        GameEventType.PLAYER_REPLACED: PlayerReplacedPayload(
            **COMMON,
            old_participant_id="participant-old",
            new_participant_id="participant-new",
            binding_version=3,
        ),
        GameEventType.SESSION_CONTROL_REJECTED: SessionControlRejectedPayload(
            **{**COMMON, "result_code": "STALE_VERSION"},
            reason_code="STALE_VERSION",
            state_version=5,
        ),
        GameEventType.PHASE_CHANGED: PhaseChangedPayload(
            **COMMON,
            previous_phase=GamePhase.EXPLORATION,
            current_phase=GamePhase.DISCUSSION,
        ),
    }


def make_result_event(event_type: GameEventType) -> GameEvent:
    payload = payloads_by_type()[event_type]
    return GameEvent(
        event_id=f"event-{event_type.value.lower()}",
        game_id="game-1",
        session_id="session-game-1",
        event_type=event_type,
        actor="session-actor",
        source=GameEventSource.CONTROL,
        correlation_id="correlation-1",
        timestamp=NOW,
        payload=payload.to_mapping(),
        schema_version=1,
        visibility=CONTROL_RESULT_VISIBILITY[event_type],
        observed_state_version=4,
        causation_event_id="event-command-1",
    )


def test_all_frozen_control_result_event_types_are_typed_and_valid() -> None:
    assert set(CONTROL_RESULT_PAYLOAD_TYPES) == {
        GameEventType.SESSION_CREATED,
        GameEventType.SESSION_STARTED,
        GameEventType.SESSION_PAUSED,
        GameEventType.SESSION_RESUMED,
        GameEventType.SESSION_ENDED,
        GameEventType.SCRIPT_SET,
        GameEventType.CHARACTER_ASSIGNED,
        GameEventType.PLAYER_REPLACED,
        GameEventType.SESSION_CONTROL_REJECTED,
        GameEventType.PHASE_CHANGED,
    }

    for event_type, expected_payload in payloads_by_type().items():
        event = make_result_event(event_type)
        assert event.event_type is event_type
        assert validate_control_result_event(event) == expected_payload


def test_extended_envelope_fields_are_available() -> None:
    event = make_result_event(GameEventType.SESSION_STARTED)

    assert event.schema_version == 1
    assert event.visibility is EventVisibility.SYSTEM_ONLY
    assert event.observed_state_version == 4
    assert event.causation_event_id == "event-command-1"


def test_existing_root_event_uses_safe_compatible_defaults() -> None:
    event = GameEvent(
        event_id="event-root",
        game_id="game-1",
        session_id="session-game-1",
        event_type=GameEventType.MESSAGE_RECEIVED,
        actor="participant-1",
        source=GameEventSource.PLATFORM,
        correlation_id="correlation-root",
        timestamp=NOW,
        payload={"content_reference": "observation-1"},
    )

    assert event.schema_version == 1
    assert event.visibility is EventVisibility.SYSTEM_ONLY
    assert event.observed_state_version is None
    assert event.causation_event_id is None


def test_control_payload_rejects_missing_or_additional_fields() -> None:
    event = make_result_event(GameEventType.CHARACTER_ASSIGNED)

    with pytest.raises(ControlResultPayloadSchemaError, match="fields"):
        validate_control_result_event(
            replace(event, payload={**event.payload, "private_knowledge": "forbidden"})
        )

    incomplete = dict(event.payload)
    incomplete.pop("binding_version")
    with pytest.raises(ControlResultPayloadSchemaError, match="fields"):
        validate_control_result_event(replace(event, payload=incomplete))


def test_control_payload_rejects_invalid_typed_values() -> None:
    event = make_result_event(GameEventType.PHASE_CHANGED)

    with pytest.raises(ControlResultPayloadSchemaError, match="invalid values"):
        validate_control_result_event(
            replace(event, payload={**event.payload, "current_phase": "NOT_A_PHASE"})
        )

    character = make_result_event(GameEventType.CHARACTER_ASSIGNED)
    with pytest.raises(ControlResultPayloadSchemaError, match="invalid values"):
        validate_control_result_event(
            replace(character, payload={**character.payload, "binding_version": 0})
        )


def test_control_schema_version_is_explicit_and_fail_closed() -> None:
    event = make_result_event(GameEventType.SESSION_PAUSED)

    with pytest.raises(ControlResultPayloadSchemaError, match="schema_version"):
        validate_control_result_event(replace(event, schema_version=2))

    with pytest.raises(ValueError, match="schema_version"):
        replace(event, schema_version=0)


def test_result_event_requires_frozen_visibility_and_causation_fields() -> None:
    event = make_result_event(GameEventType.PHASE_CHANGED)

    with pytest.raises(ControlResultPayloadSchemaError, match="visibility"):
        validate_control_result_event(
            replace(event, visibility=EventVisibility.SYSTEM_ONLY)
        )
    with pytest.raises(ControlResultPayloadSchemaError, match="causation_event_id"):
        validate_control_result_event(replace(event, causation_event_id=None))
    with pytest.raises(ControlResultPayloadSchemaError, match="observed_state_version"):
        validate_control_result_event(replace(event, observed_state_version=None))
