from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timezone

import pytest

import game_runtime.event as game_event
import game_runtime.session_control as session_control
from game_runtime.event import EventVisibility, GameEvent, GameEventSource
from game_runtime.session_control import (
    ConfirmationRequirement,
    SessionControlPermission,
)


def _contract(module: object, name: str):
    value = getattr(module, name, None)
    assert value is not None, f"missing contract: {name}"
    return value


def _available_evidence(**overrides: object):
    evidence_type = _contract(
        session_control,
        "ControlQuestActivationEvidence",
    )
    disposition = _contract(
        session_control,
        "QuestActivationDisposition",
    )
    status = _contract(session_control, "QuestEvidenceStatus")
    values: dict[str, object] = {
        "evidence_schema_version": 1,
        "game_id": "game-1",
        "session_id": "session-1",
        "observed_state_version": 4,
        "quest_id": "quest-1",
        "active_rule_set_reference": "rule-set:commit-1",
        "current_active_quest_id": None,
        "current_public_state_reference": None,
        "resulting_public_state_reference": "quest-public:commit-1",
        "current_hidden_state_reference": "hidden-state:commit-1",
        "resulting_hidden_state_reference": "hidden-state:commit-2",
        "game_rule_version": 1,
        "quest_version": 0,
        "hidden_state_version": 1,
        "provenance_reference": "quest-provenance:1",
        "disposition": disposition.AVAILABLE,
        "validation_status": status.VERIFIED,
    }
    values.update(overrides)
    return evidence_type(**values)


def test_activate_quest_command_payload_is_closed_and_immutable() -> None:
    command_type = _contract(session_control, "SessionCommandType")
    payload_type = _contract(session_control, "ActivateQuestPayload")
    payload = payload_type("quest-1", 1, 0, 1)

    assert command_type.ACTIVATE_QUEST.value == "ACTIVATE_QUEST"
    assert tuple(field.name for field in fields(payload_type)) == (
        "quest_id",
        "expected_game_rule_version",
        "expected_quest_version",
        "expected_hidden_state_version",
    )
    assert not hasattr(payload, "__dict__")
    with pytest.raises(FrozenInstanceError):
        payload.quest_id = "quest-other"
    with pytest.raises(ValueError):
        payload_type("", 1, 0, 1)
    with pytest.raises(ValueError):
        payload_type("quest-1", 1, -1, 1)


def test_activate_quest_is_dm_only_high_risk_control() -> None:
    command_type = _contract(session_control, "SessionCommandType")
    payload = session_control.ActivateQuestPayload("quest-1", 1, 0, 1)
    command = session_control.SessionCommand(
        command_id="command-1",
        command_type=command_type.ACTIVATE_QUEST,
        requester="dm-1",
        group_id="group-1",
        game_id="game-1",
        session_id="session-1",
        requester_binding_version=1,
        observed_state_version=4,
        payload=payload,
        correlation_id="correlation-1",
        requested_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    assert (
        session_control.required_permission_for(command_type.ACTIVATE_QUEST)
        is SessionControlPermission.ACTIVATE_QUEST
    )
    assert (
        session_control.confirmation_requirement_for(
            command
        )
        is ConfirmationRequirement.REQUIRED
    )


def test_available_quest_evidence_is_frozen_slotted_and_payload_bound() -> None:
    evidence = _available_evidence()
    payload = _contract(session_control, "ActivateQuestPayload")(
        "quest-1",
        1,
        0,
        1,
    )

    assert not hasattr(evidence, "__dict__")
    with pytest.raises(FrozenInstanceError):
        evidence.quest_id = "quest-other"
    evidence.validate_for_command(
        session_control.SessionCommandType.ACTIVATE_QUEST,
        game_id="game-1",
        session_id="session-1",
        observed_state_version=4,
    )
    evidence.validate_payload_binding(payload)


@pytest.mark.parametrize(
    "overrides",
    [
        {"resulting_public_state_reference": None},
        {"resulting_hidden_state_reference": "hidden-state:commit-1"},
        {"provenance_reference": None},
        {"quest_version": 1},
    ],
)
def test_available_evidence_rejects_partial_or_non_initial_state(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _available_evidence(**overrides)


@pytest.mark.parametrize(
    "disposition_name",
    [
        "ALREADY_ACTIVE",
        "CONFLICT",
        "NOT_FOUND",
        "NOT_ACTIVATABLE",
        "RULE_SET_NOT_ACTIVE",
    ],
)
def test_business_dispositions_are_exact_and_non_speculative(
    disposition_name: str,
) -> None:
    disposition = _contract(
        session_control,
        "QuestActivationDisposition",
    )
    active = disposition_name not in {
        "NOT_FOUND",
        "NOT_ACTIVATABLE",
        "RULE_SET_NOT_ACTIVE",
    }
    rule_active = disposition_name != "RULE_SET_NOT_ACTIVE"
    evidence = _available_evidence(
        active_rule_set_reference=("rule-set:commit-1" if rule_active else None),
        current_active_quest_id=("quest-1" if active else None),
        current_public_state_reference=(
            "quest-public:current" if active else None
        ),
        resulting_public_state_reference=None,
        current_hidden_state_reference=(
            "hidden-state:commit-1" if rule_active else None
        ),
        resulting_hidden_state_reference=None,
        game_rule_version=(1 if rule_active else 0),
        quest_version=(1 if active else 0),
        hidden_state_version=(1 if rule_active else 0),
        provenance_reference=None,
        disposition=getattr(disposition, disposition_name),
    )

    assert evidence.disposition.value == disposition_name


def test_unknown_evidence_is_representable_but_fails_closed() -> None:
    disposition = _contract(
        session_control,
        "QuestActivationDisposition",
    )
    evidence = _available_evidence(
        resulting_public_state_reference=None,
        resulting_hidden_state_reference=None,
        provenance_reference=None,
        disposition=disposition.UNKNOWN,
    )

    with pytest.raises(ValueError, match="UNKNOWN"):
        evidence.validate_for_command(
            session_control.SessionCommandType.ACTIVATE_QUEST,
            game_id="game-1",
            session_id="session-1",
            observed_state_version=4,
        )


def test_quest_event_is_public_and_does_not_expose_hidden_evidence() -> None:
    payload_type = _contract(game_event, "QuestActivatedPayload")
    event_type = _contract(game_event, "GameEventType")
    payload = payload_type(
        command_id="command-1",
        operation_id="operation-1",
        input_event_id="event-7",
        result_code="QUEST_ACTIVATED",
        result_state_version=5,
        quest_id="quest-1",
        public_state_reference="quest-public:commit-1",
        quest_domain_version=1,
    )
    event = GameEvent(
        event_id="result-1",
        game_id="game-1",
        session_id="session-1",
        event_type=event_type.QUEST_ACTIVATED,
        actor="session-actor",
        source=GameEventSource.CONTROL,
        correlation_id="correlation-1",
        timestamp=datetime(2026, 8, 8, tzinfo=timezone.utc),
        payload=payload.to_mapping(),
        visibility=EventVisibility.PUBLIC,
        observed_state_version=4,
        causation_event_id="event-7",
    )

    validated = game_event.validate_control_result_event(event)
    assert validated == payload
    assert game_event.CONTROL_RESULT_VISIBILITY[event_type.QUEST_ACTIVATED] is (
        EventVisibility.PUBLIC
    )
    surface = repr(payload)
    assert "hidden" not in surface.casefold()
    assert "provenance" not in surface.casefold()
