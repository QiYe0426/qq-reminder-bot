from datetime import datetime, timezone

import pytest

from game_runtime.participant import (
    ParticipantMembershipState,
    ParticipantType,
)
from game_runtime.session import GameSessionStatus
from game_runtime.session_control import (
    ALL_SESSION_CONTROL_PERMISSIONS,
    AuthorizationContext,
    AuthorizationDecision,
    AuthorizationReason,
    CreateSessionBootstrapContext,
    ExistingSessionContext,
    SessionCommand,
    SessionCommandPayload,
    SessionCommandType,
    SessionControlAuthorizationPolicy,
    SessionControlPermission,
    SessionRoleBinding,
    required_permission_for,
)


REQUESTED_AT = datetime(2026, 7, 15, tzinfo=timezone.utc)


def make_resolution(
    session,
    command_type: SessionCommandType = SessionCommandType.START_GAME,
    payload: SessionCommandPayload | None = None,
) -> ExistingSessionContext:
    command = SessionCommand(
        command_id=f"command-{command_type.value.lower()}",
        command_type=command_type,
        requester="principal-dm-1",
        game_id=session.game_id,
        session_id=session.session_id,
        group_id=session.group_id,
        requester_binding_version=1,
        observed_state_version=session.state_version,
        payload=payload or _payload_for(command_type),
        correlation_id=f"correlation-{command_type.value.lower()}",
        requested_at=REQUESTED_AT,
    )
    return ExistingSessionContext(command=command, session=session)


def _payload_for(command_type: SessionCommandType) -> SessionCommandPayload:
    from game_runtime.session_control import (
        AssignCharacterPayload,
        ChangePhasePayload,
        EndGamePayload,
        PauseGamePayload,
        ReplacePlayerPayload,
        ResumeGamePayload,
        SetScriptPayload,
        StartGamePayload,
    )
    from game_runtime.session import GamePhase

    payloads = {
        SessionCommandType.START_GAME: StartGamePayload(),
        SessionCommandType.PAUSE_GAME: PauseGamePayload(reason_code="DM_REQUEST"),
        SessionCommandType.RESUME_GAME: ResumeGamePayload(),
        SessionCommandType.END_GAME: EndGamePayload(),
        SessionCommandType.CHANGE_PHASE: ChangePhasePayload(
            target_phase=GamePhase.DISCUSSION
        ),
        SessionCommandType.SET_SCRIPT: SetScriptPayload(
            script_id="script-1",
            public_name="Script",
            manifest_reference="manifest-1",
        ),
        SessionCommandType.ASSIGN_CHARACTER: AssignCharacterPayload(
            participant_id="participant-player",
            character_id="character-1",
        ),
        SessionCommandType.REPLACE_PLAYER: ReplacePlayerPayload(
            old_participant_id="participant-old",
            new_participant_id="participant-new",
            expected_binding_version=1,
        ),
    }
    return payloads[command_type]


def make_dm_binding(session, **overrides: object) -> SessionRoleBinding:
    values: dict[str, object] = {
        "principal_id": "principal-dm-1",
        "game_id": session.game_id,
        "session_id": session.session_id,
        "participant_id": "participant-dm",
        "participant_type": ParticipantType.DM,
        "membership_state": ParticipantMembershipState.ACTIVE,
        "binding_version": 1,
        "granted_permissions": ALL_SESSION_CONTROL_PERMISSIONS,
    }
    values.update(overrides)
    return SessionRoleBinding(**values)  # type: ignore[arg-type]


def make_context(session, **overrides: object) -> AuthorizationContext:
    values: dict[str, object] = {
        "global_gate_allowed": True,
        "game_mode_permissions": ALL_SESSION_CONTROL_PERMISSIONS,
        "role_binding": make_dm_binding(session),
    }
    values.update(overrides)
    return AuthorizationContext(**values)  # type: ignore[arg-type]


def test_active_session_dm_is_allowed(session_factory) -> None:
    session = session_factory()
    result = SessionControlAuthorizationPolicy().authorize(
        make_resolution(session),
        make_context(session),
    )

    assert result.decision is AuthorizationDecision.ALLOW
    assert result.reason is AuthorizationReason.ALLOWED


def test_non_dm_is_denied(session_factory) -> None:
    session = session_factory()
    context = make_context(
        session,
        role_binding=make_dm_binding(
            session,
            participant_type=ParticipantType.PLAYER,
        ),
    )

    result = SessionControlAuthorizationPolicy().authorize(
        make_resolution(session),
        context,
    )

    assert result.decision is AuthorizationDecision.DENY
    assert result.reason is AuthorizationReason.SESSION_ROLE_DENIED


def test_requester_identity_mismatch_is_denied(session_factory) -> None:
    session = session_factory()
    context = make_context(
        session,
        role_binding=make_dm_binding(session, principal_id="principal-other"),
    )

    result = SessionControlAuthorizationPolicy().authorize(
        make_resolution(session),
        context,
    )

    assert result.reason is AuthorizationReason.REQUESTER_MISMATCH


def test_inactive_participant_is_denied(session_factory) -> None:
    session = session_factory()
    context = make_context(
        session,
        role_binding=make_dm_binding(
            session,
            membership_state=ParticipantMembershipState.REVOKED,
        ),
    )

    result = SessionControlAuthorizationPolicy().authorize(
        make_resolution(session),
        context,
    )

    assert result.reason is AuthorizationReason.INACTIVE_PARTICIPANT


def test_binding_version_mismatch_is_denied(session_factory) -> None:
    session = session_factory()
    context = make_context(
        session,
        role_binding=make_dm_binding(session, binding_version=2),
    )

    result = SessionControlAuthorizationPolicy().authorize(
        make_resolution(session),
        context,
    )

    assert result.reason is AuthorizationReason.BINDING_VERSION_MISMATCH


def test_dm_from_another_session_is_denied(session_factory) -> None:
    session = session_factory()
    context = make_context(
        session,
        role_binding=make_dm_binding(session, session_id="session-other"),
    )

    result = SessionControlAuthorizationPolicy().authorize(
        make_resolution(session),
        context,
    )

    assert result.reason is AuthorizationReason.SESSION_SCOPE_MISMATCH


def test_global_and_game_mode_gates_are_intersected(session_factory) -> None:
    session = session_factory()
    policy = SessionControlAuthorizationPolicy()
    resolution = make_resolution(session)

    global_denied = policy.authorize(
        resolution,
        make_context(session, global_gate_allowed=False),
    )
    game_denied = policy.authorize(
        resolution,
        make_context(session, game_mode_permissions=frozenset()),
    )

    assert global_denied.reason is AuthorizationReason.GLOBAL_GATE_DENIED
    assert game_denied.reason is AuthorizationReason.GAME_MODE_PERMISSION_DENIED


def test_create_requires_bootstrap_controller_role() -> None:
    resolution = CreateSessionBootstrapContext(
        group_id="group-1",
        requester="principal-dm-1",
        command_id="command-create",
        correlation_id="correlation-create",
        requested_at=REQUESTED_AT,
        proposed_game_id="game-proposed",
        proposed_session_id="session-proposed",
    )
    policy = SessionControlAuthorizationPolicy()
    base = AuthorizationContext(
        global_gate_allowed=True,
        game_mode_permissions=ALL_SESSION_CONTROL_PERMISSIONS,
    )

    denied = policy.authorize(resolution, base)
    allowed = policy.authorize(
        resolution,
        AuthorizationContext(
            global_gate_allowed=True,
            game_mode_permissions=ALL_SESSION_CONTROL_PERMISSIONS,
            bootstrap_controller=True,
        ),
    )

    assert denied.reason is AuthorizationReason.SESSION_ROLE_DENIED
    assert allowed.decision is AuthorizationDecision.ALLOW


@pytest.mark.parametrize("command_type", list(SessionCommandType))
def test_every_command_has_a_typed_permission_mapping(
    command_type: SessionCommandType,
) -> None:
    permission = required_permission_for(command_type)

    assert isinstance(permission, SessionControlPermission)
    assert permission.value == command_type.value


@pytest.mark.parametrize(
    ("command_type", "status"),
    [
        (SessionCommandType.START_GAME, GameSessionStatus.RUNNING),
        (SessionCommandType.PAUSE_GAME, GameSessionStatus.CREATED),
        (SessionCommandType.RESUME_GAME, GameSessionStatus.RUNNING),
        (SessionCommandType.END_GAME, GameSessionStatus.ENDED),
        (SessionCommandType.CHANGE_PHASE, GameSessionStatus.PAUSED),
        (SessionCommandType.SET_SCRIPT, GameSessionStatus.RUNNING),
        (SessionCommandType.ASSIGN_CHARACTER, GameSessionStatus.RUNNING),
        (SessionCommandType.REPLACE_PLAYER, GameSessionStatus.RUNNING),
    ],
)
def test_lifecycle_constraint_denies_invalid_status(
    session_factory,
    command_type: SessionCommandType,
    status: GameSessionStatus,
) -> None:
    session = session_factory()
    if status is GameSessionStatus.RUNNING:
        session.transition_to(GameSessionStatus.RUNNING)
    elif status is GameSessionStatus.PAUSED:
        session.transition_to(GameSessionStatus.RUNNING)
        session.transition_to(GameSessionStatus.PAUSED)
    elif status is GameSessionStatus.ENDED:
        session.transition_to(GameSessionStatus.ENDED)
    resolution = make_resolution(session, command_type)

    result = SessionControlAuthorizationPolicy().authorize(
        resolution,
        make_context(session),
    )

    assert result.reason is AuthorizationReason.COMMAND_CONSTRAINT_DENIED


def test_precomputed_confirmation_requirement_is_preserved(session_factory) -> None:
    session = session_factory()
    result = SessionControlAuthorizationPolicy().authorize(
        make_resolution(session),
        make_context(session, confirmation_required=True),
    )

    assert result.decision is AuthorizationDecision.REQUIRE_CONFIRMATION
    assert result.reason is AuthorizationReason.CONFIRMATION_REQUIRED


def test_authorization_does_not_mutate_session_or_command(session_factory) -> None:
    session = session_factory()
    resolution = make_resolution(session)
    original_command: SessionCommand = resolution.command

    SessionControlAuthorizationPolicy().authorize(
        resolution,
        make_context(session),
    )

    assert session.status is GameSessionStatus.CREATED
    assert session.state_version == 0
    assert resolution.command == original_command
