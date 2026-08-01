from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone

import pytest

from game_runtime.session import GamePhase
from game_runtime.session_control import (
    ActivateRuleSetPayload,
    AssignCharacterPayload,
    ChangePhasePayload,
    CreateSessionPayload,
    EndGamePayload,
    PauseGamePayload,
    ReplacePlayerPayload,
    ResumeGamePayload,
    SessionCommand,
    SessionCommandPayload,
    SessionCommandType,
    SetScriptPayload,
    StartGamePayload,
)


REQUESTED_AT = datetime(2026, 7, 15, tzinfo=timezone.utc)


def make_command(
    command_type: SessionCommandType,
    payload: SessionCommandPayload,
    **overrides: object,
) -> SessionCommand:
    values: dict[str, object] = {
        "command_id": f"command-{command_type.value.lower()}",
        "command_type": command_type,
        "requester": "principal-dm-1",
        "group_id": "group-1",
        "game_id": "game-1",
        "session_id": "session-1",
        "requester_binding_version": 1,
        "observed_state_version": 3,
        "payload": payload,
        "correlation_id": f"correlation-{command_type.value.lower()}",
        "requested_at": REQUESTED_AT,
    }
    if command_type is SessionCommandType.CREATE_SESSION:
        values.update(
            game_id=None,
            session_id=None,
            requester_binding_version=None,
            observed_state_version=None,
        )
    values.update(overrides)
    return SessionCommand(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("command_type", "payload"),
    [
        (
            SessionCommandType.CREATE_SESSION,
            CreateSessionPayload(prospective_dm_id="participant-dm-1"),
        ),
        (SessionCommandType.START_GAME, StartGamePayload()),
        (
            SessionCommandType.PAUSE_GAME,
            PauseGamePayload(reason_code="DM_REQUEST"),
        ),
        (SessionCommandType.RESUME_GAME, ResumeGamePayload()),
        (SessionCommandType.END_GAME, EndGamePayload()),
        (
            SessionCommandType.CHANGE_PHASE,
            ChangePhasePayload(target_phase=GamePhase.DISCUSSION),
        ),
        (
            SessionCommandType.SET_SCRIPT,
            SetScriptPayload(
                script_id="script-1",
                public_name="Public Script",
                manifest_reference="manifest-1",
            ),
        ),
        (
            SessionCommandType.ASSIGN_CHARACTER,
            AssignCharacterPayload(
                participant_id="participant-1",
                character_id="character-1",
            ),
        ),
        (
            SessionCommandType.REPLACE_PLAYER,
            ReplacePlayerPayload(
                old_participant_id="participant-old",
                new_participant_id="participant-new",
                expected_binding_version=2,
            ),
        ),
        (
            SessionCommandType.ACTIVATE_RULE_SET,
            ActivateRuleSetPayload(
                manifest_reference="manifest-1",
                expected_setup_version=1,
                expected_game_rule_version=2,
                expected_hidden_state_version=3,
            ),
        ),
    ],
)
def test_all_frozen_command_types_can_be_created(
    command_type: SessionCommandType,
    payload: SessionCommandPayload,
) -> None:
    command = make_command(command_type, payload)

    assert command.command_type is command_type
    assert command.payload is payload


def test_invalid_command_type_is_rejected() -> None:
    with pytest.raises(TypeError, match="command_type"):
        make_command(
            SessionCommandType.START_GAME,
            StartGamePayload(),
            command_type="START_GAME",
        )


def test_unknown_command_enum_value_is_rejected() -> None:
    with pytest.raises(ValueError, match="DELETE_SESSION"):
        SessionCommandType("DELETE_SESSION")


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("command_id", "", "command_id"),
        ("requester", "", "requester"),
        ("group_id", "", "group_id"),
        ("game_id", None, "game_id"),
        ("session_id", None, "session_id"),
        ("requester_binding_version", None, "requester_binding_version"),
        ("requester_binding_version", -1, "requester_binding_version"),
        ("observed_state_version", None, "observed_state_version"),
        ("observed_state_version", -1, "observed_state_version"),
        ("correlation_id", "", "correlation_id"),
        (
            "requested_at",
            datetime(2026, 7, 15),
            "timezone-aware",
        ),
    ],
)
def test_envelope_fields_are_validated(
    field: str,
    value: object,
    error: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=error):
        make_command(
            SessionCommandType.START_GAME,
            StartGamePayload(),
            **{field: value},
        )


def test_create_session_rejects_existing_scope() -> None:
    with pytest.raises(ValueError, match="CREATE_SESSION"):
        make_command(
            SessionCommandType.CREATE_SESSION,
            CreateSessionPayload(prospective_dm_id="participant-dm-1"),
            game_id="game-existing",
        )


def test_non_payload_value_is_rejected() -> None:
    with pytest.raises(TypeError, match="payload"):
        make_command(
            SessionCommandType.START_GAME,
            {"unsafe": "free-form"},  # type: ignore[arg-type]
        )


def test_payload_for_another_command_is_rejected() -> None:
    with pytest.raises(TypeError, match="StartGamePayload"):
        make_command(
            SessionCommandType.START_GAME,
            ResumeGamePayload(),
        )


def test_activate_rule_set_payload_is_frozen_slotted_and_has_exact_fields() -> None:
    payload = ActivateRuleSetPayload(
        manifest_reference="manifest-1",
        expected_setup_version=1,
        expected_game_rule_version=2,
        expected_hidden_state_version=3,
    )

    assert [field.name for field in fields(ActivateRuleSetPayload)] == [
        "manifest_reference",
        "expected_setup_version",
        "expected_game_rule_version",
        "expected_hidden_state_version",
    ]
    assert not hasattr(payload, "__dict__")
    with pytest.raises(FrozenInstanceError):
        payload.manifest_reference = "manifest-other"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"manifest_reference": ""}, "manifest_reference"),
        ({"expected_setup_version": -1}, "expected_setup_version"),
        ({"expected_game_rule_version": True}, "expected_game_rule_version"),
        ({"expected_hidden_state_version": "3"}, "expected_hidden_state_version"),
    ],
)
def test_activate_rule_set_payload_rejects_invalid_fields(
    kwargs: dict[str, object], error: str
) -> None:
    values: dict[str, object] = {
        "manifest_reference": "manifest-1",
        "expected_setup_version": 1,
        "expected_game_rule_version": 2,
        "expected_hidden_state_version": 3,
    }
    values.update(kwargs)

    with pytest.raises((TypeError, ValueError), match=error):
        ActivateRuleSetPayload(**values)  # type: ignore[arg-type]
