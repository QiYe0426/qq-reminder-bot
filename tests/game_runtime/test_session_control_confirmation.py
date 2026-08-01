from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from game_runtime.session import GamePhase
from game_runtime.session_control import (
    ActivateRuleSetPayload,
    AssignCharacterPayload,
    ChangePhasePayload,
    ConfirmationInvalidReason,
    ConfirmationPolicyContext,
    ConfirmationRequirement,
    ConfirmationStatus,
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
    confirmation_requirement_for,
    create_confirmation,
    validate_confirmation,
)


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


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
        "observed_state_version": 7,
        "payload": payload,
        "correlation_id": f"correlation-{command_type.value.lower()}",
        "requested_at": NOW,
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


def make_end_command(**overrides: object) -> SessionCommand:
    return make_command(
        SessionCommandType.END_GAME,
        EndGamePayload(public_result_reference="result-1"),
        **overrides,
    )


def make_confirmation(command: SessionCommand):
    return create_confirmation(
        command,
        confirmation_id="confirmation-1",
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )


@pytest.mark.parametrize(
    ("command_type", "payload"),
    [
        (SessionCommandType.START_GAME, StartGamePayload()),
        (SessionCommandType.RESUME_GAME, ResumeGamePayload()),
        (SessionCommandType.END_GAME, EndGamePayload()),
        (
            SessionCommandType.ACTIVATE_RULE_SET,
            ActivateRuleSetPayload(
                manifest_reference="manifest-1",
                expected_setup_version=1,
                expected_game_rule_version=2,
                expected_hidden_state_version=3,
            ),
        ),
        (
            SessionCommandType.REPLACE_PLAYER,
            ReplacePlayerPayload(
                old_participant_id="participant-old",
                new_participant_id="participant-new",
                expected_binding_version=1,
            ),
        ),
    ],
)
def test_high_risk_commands_require_confirmation(
    command_type: SessionCommandType,
    payload: SessionCommandPayload,
) -> None:
    command = make_command(command_type, payload)

    assert (
        confirmation_requirement_for(command)
        is ConfirmationRequirement.REQUIRED
    )


@pytest.mark.parametrize(
    ("command_type", "payload"),
    [
        (
            SessionCommandType.PAUSE_GAME,
            PauseGamePayload(reason_code="DM_REQUEST"),
        ),
        (
            SessionCommandType.CREATE_SESSION,
            CreateSessionPayload(prospective_dm_id="participant-dm-1"),
        ),
    ],
)
def test_low_risk_commands_do_not_require_confirmation(
    command_type: SessionCommandType,
    payload: SessionCommandPayload,
) -> None:
    command = make_command(command_type, payload)

    assert (
        confirmation_requirement_for(command)
        is ConfirmationRequirement.NOT_REQUIRED
    )


def test_setup_overwrite_and_critical_phase_policy() -> None:
    script = make_command(
        SessionCommandType.SET_SCRIPT,
        SetScriptPayload("script-1", "Script", "manifest-1"),
    )
    character = make_command(
        SessionCommandType.ASSIGN_CHARACTER,
        AssignCharacterPayload("participant-1", "character-1"),
    )
    voting = make_command(
        SessionCommandType.CHANGE_PHASE,
        ChangePhasePayload(GamePhase.VOTING),
    )
    discussion = make_command(
        SessionCommandType.CHANGE_PHASE,
        ChangePhasePayload(GamePhase.DISCUSSION),
    )

    assert confirmation_requirement_for(
        script,
        ConfirmationPolicyContext(script_already_set=True),
    ) is ConfirmationRequirement.REQUIRED
    assert (
        confirmation_requirement_for(script)
        is ConfirmationRequirement.NOT_REQUIRED
    )
    assert confirmation_requirement_for(
        character,
        ConfirmationPolicyContext(character_binding_exists=True),
    ) is ConfirmationRequirement.REQUIRED
    assert (
        confirmation_requirement_for(character)
        is ConfirmationRequirement.NOT_REQUIRED
    )
    assert (
        confirmation_requirement_for(voting)
        is ConfirmationRequirement.REQUIRED
    )
    ending = make_command(
        SessionCommandType.CHANGE_PHASE,
        ChangePhasePayload(GamePhase.ENDING),
    )
    assert (
        confirmation_requirement_for(ending)
        is ConfirmationRequirement.REQUIRED
    )
    assert (
        confirmation_requirement_for(discussion)
        is ConfirmationRequirement.NOT_REQUIRED
    )


def test_valid_confirmation_is_consumed() -> None:
    command = make_end_command()
    confirmation = make_confirmation(command)

    result = validate_confirmation(
        confirmation,
        command,
        validated_at=NOW + timedelta(minutes=1),
    )

    assert result.valid
    assert result.reason is None
    assert confirmation.status is ConfirmationStatus.CONFIRMED


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"command_id": "command-other"},
            ConfirmationInvalidReason.COMMAND_ID_MISMATCH,
        ),
        (
            {"game_id": "game-other"},
            ConfirmationInvalidReason.GAME_ID_MISMATCH,
        ),
        (
            {"session_id": "session-other"},
            ConfirmationInvalidReason.SESSION_ID_MISMATCH,
        ),
        (
            {"requester": "principal-other"},
            ConfirmationInvalidReason.REQUESTER_MISMATCH,
        ),
        (
            {"observed_state_version": 8},
            ConfirmationInvalidReason.STATE_VERSION_MISMATCH,
        ),
    ],
)
def test_changed_command_binding_is_rejected(
    overrides: dict[str, object],
    reason: ConfirmationInvalidReason,
) -> None:
    original = make_end_command()
    confirmation = make_confirmation(original)
    changed = replace(original, **overrides)

    result = validate_confirmation(
        confirmation,
        changed,
        validated_at=NOW + timedelta(minutes=1),
    )

    assert not result.valid
    assert result.reason is reason
    assert confirmation.status is ConfirmationStatus.PENDING


def test_changed_command_type_is_rejected() -> None:
    original = make_end_command()
    confirmation = make_confirmation(original)
    changed = make_command(
        SessionCommandType.REPLACE_PLAYER,
        ReplacePlayerPayload(
            old_participant_id="participant-old",
            new_participant_id="participant-new",
            expected_binding_version=1,
        ),
        command_id=original.command_id,
    )

    result = validate_confirmation(
        confirmation,
        changed,
        validated_at=NOW + timedelta(minutes=1),
    )

    assert result.reason is ConfirmationInvalidReason.COMMAND_TYPE_MISMATCH


def test_payload_change_is_rejected() -> None:
    original = make_end_command()
    confirmation = make_confirmation(original)
    changed = replace(
        original,
        payload=EndGamePayload(public_result_reference="result-other"),
    )

    result = validate_confirmation(
        confirmation,
        changed,
        validated_at=NOW + timedelta(minutes=1),
    )

    assert result.reason is ConfirmationInvalidReason.PAYLOAD_FINGERPRINT_MISMATCH


def test_activate_rule_set_confirmation_binds_the_payload_fingerprint() -> None:
    original = make_command(
        SessionCommandType.ACTIVATE_RULE_SET,
        ActivateRuleSetPayload(
            manifest_reference="manifest-1",
            expected_setup_version=1,
            expected_game_rule_version=2,
            expected_hidden_state_version=3,
        ),
    )
    confirmation = make_confirmation(original)
    changed = replace(
        original,
        payload=ActivateRuleSetPayload(
            manifest_reference="manifest-1",
            expected_setup_version=1,
            expected_game_rule_version=3,
            expected_hidden_state_version=3,
        ),
    )

    assert confirmation.payload_fingerprint != ""
    result = validate_confirmation(
        confirmation,
        changed,
        validated_at=NOW + timedelta(minutes=1),
    )

    assert result.reason is ConfirmationInvalidReason.PAYLOAD_FINGERPRINT_MISMATCH


def test_expired_confirmation_is_rejected() -> None:
    command = make_end_command()
    confirmation = make_confirmation(command)

    result = validate_confirmation(
        confirmation,
        command,
        validated_at=NOW + timedelta(minutes=5),
    )

    assert result.reason is ConfirmationInvalidReason.EXPIRED
    assert confirmation.status is ConfirmationStatus.EXPIRED


def test_confirmed_confirmation_cannot_be_reused() -> None:
    command = make_end_command()
    confirmation = make_confirmation(command)
    first = validate_confirmation(
        confirmation,
        command,
        validated_at=NOW + timedelta(minutes=1),
    )

    second = validate_confirmation(
        confirmation,
        command,
        validated_at=NOW + timedelta(minutes=2),
    )

    assert first.valid
    assert not second.valid
    assert second.reason is ConfirmationInvalidReason.ALREADY_CONFIRMED
