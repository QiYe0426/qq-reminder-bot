import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from game_runtime.actor import GameSessionActor
from game_runtime.event import GameEvent, GameEventSource, GameEventType
from game_runtime.routing import ActorGameRuntimeIngress
from game_runtime.session import GameSessionStatus
from game_runtime.session_control import (
    AuthorizationDecision,
    AuthorizationReason,
    AuthorizationResult,
    CommandEventConversion,
    CommandGovernanceRejected,
    ControlOperationLink,
    DMCommandEventSchemaError,
    EndGamePayload,
    PauseGamePayload,
    SessionCommand,
    SessionCommandEventConverter,
    SessionCommandType,
    SessionControlEventIngress,
    SessionControlPermission,
    create_confirmation,
    validate_confirmation,
    validate_dm_command_event,
)


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def run(coroutine):
    return asyncio.run(coroutine)


class RecordingEventStore:
    def __init__(self) -> None:
        self.events: list[GameEvent] = []

    async def append_event(self, event: GameEvent) -> object:
        self.events.append(event)
        return {"sequence_no": len(self.events)}


def make_command(
    command_type: SessionCommandType = SessionCommandType.PAUSE_GAME,
) -> SessionCommand:
    payload = (
        PauseGamePayload(reason_code="DM_REQUEST")
        if command_type is SessionCommandType.PAUSE_GAME
        else EndGamePayload(public_result_reference="result-1")
    )
    return SessionCommand(
        command_id=f"command-{command_type.value.lower()}",
        command_type=command_type,
        requester="principal-dm-1",
        group_id="group-1",
        game_id="game-1",
        session_id="session-game-1",
        requester_binding_version=1,
        observed_state_version=4,
        payload=payload,
        correlation_id=f"correlation-{command_type.value.lower()}",
        requested_at=NOW,
    )


def authorization_for(
    command: SessionCommand,
    decision: AuthorizationDecision = AuthorizationDecision.ALLOW,
) -> AuthorizationResult:
    reason = (
        AuthorizationReason.ALLOWED
        if decision is AuthorizationDecision.ALLOW
        else AuthorizationReason.GLOBAL_GATE_DENIED
    )
    if decision is AuthorizationDecision.REQUIRE_CONFIRMATION:
        reason = AuthorizationReason.CONFIRMATION_REQUIRED
    return AuthorizationResult(
        decision=decision,
        reason=reason,
        required_permission=SessionControlPermission(command.command_type.value),
    )


def convert_pause(command: SessionCommand | None = None) -> CommandEventConversion:
    target = command or make_command()
    return SessionCommandEventConverter().convert(
        target,
        authorization_for(target),
        event_id="event-command-1",
        causation_event_id="event-input-1",
    )


def test_command_converts_to_existing_dm_command_event() -> None:
    command = make_command()
    conversion = convert_pause(command)
    event = conversion.event
    payload = validate_dm_command_event(event)

    assert event.event_type is GameEventType.DM_COMMAND
    assert event.source is GameEventSource.CONTROL
    assert event.game_id == command.game_id
    assert event.session_id == command.session_id
    assert payload.command_id == command.command_id
    assert payload.requester == command.requester
    assert event.correlation_id == command.correlation_id
    assert payload.observed_state_version == command.observed_state_version
    assert payload.payload_reference == f"sha256:{payload.payload_fingerprint}"
    assert conversion.operation_link == ControlOperationLink(
        command_id=command.command_id,
        event_id=event.event_id,
        correlation_id=command.correlation_id,
    )


def test_denied_command_does_not_generate_event() -> None:
    command = make_command()

    with pytest.raises(CommandGovernanceRejected, match="authorization"):
        SessionCommandEventConverter().convert(
            command,
            authorization_for(command, AuthorizationDecision.DENY),
            event_id="event-command-1",
            causation_event_id="event-input-1",
        )


def test_required_confirmation_must_be_valid() -> None:
    command = make_command(SessionCommandType.END_GAME)
    converter = SessionCommandEventConverter()
    authorization = authorization_for(
        command,
        AuthorizationDecision.REQUIRE_CONFIRMATION,
    )

    with pytest.raises(CommandGovernanceRejected, match="confirmation"):
        converter.convert(
            command,
            authorization,
            event_id="event-command-1",
            causation_event_id="event-input-1",
        )

    confirmation = create_confirmation(
        command,
        confirmation_id="confirmation-1",
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    validation = validate_confirmation(
        confirmation,
        command,
        validated_at=NOW + timedelta(minutes=1),
    )
    assert validation.valid
    conversion = converter.convert(
        command,
        authorization,
        event_id="event-command-1",
        causation_event_id="event-input-1",
        confirmation=confirmation,
    )

    assert conversion.event.event_type is GameEventType.DM_COMMAND


def test_confirmation_for_another_command_is_rejected() -> None:
    command = make_command(SessionCommandType.END_GAME)
    other = replace(command, command_id="command-other")
    confirmation = create_confirmation(
        other,
        confirmation_id="confirmation-other",
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    assert validate_confirmation(
        confirmation,
        other,
        validated_at=NOW + timedelta(minutes=1),
    ).valid

    with pytest.raises(CommandGovernanceRejected, match="confirmation"):
        SessionCommandEventConverter().convert(
            command,
            authorization_for(
                command,
                AuthorizationDecision.REQUIRE_CONFIRMATION,
            ),
            event_id="event-command-1",
            causation_event_id="event-input-1",
            confirmation=confirmation,
        )


def test_event_schema_validation_rejects_invalid_payload() -> None:
    valid = convert_pause().event
    invalid = replace(
        valid,
        payload={**valid.payload, "visibility": "PUBLIC"},
    )

    with pytest.raises(DMCommandEventSchemaError):
        validate_dm_command_event(invalid)


def test_ingest_delivers_event_without_modifying_session(session_factory) -> None:
    session = session_factory("game-1", "group-1")
    session.transition_to(GameSessionStatus.RUNNING)
    status_before = session.status
    phase_before = session.current_phase
    version_before = session.state_version
    actor = GameSessionActor(session)
    mailbox = ActorGameRuntimeIngress()
    mailbox.register(actor)
    event_store = RecordingEventStore()
    ingress = SessionControlEventIngress(event_store, mailbox)
    conversion = convert_pause()

    result = run(ingress.ingest(conversion))

    assert result.delivered
    assert event_store.events == [conversion.event]
    assert actor.processed_event_ids == (conversion.event.event_id,)
    assert session.status is status_before
    assert session.current_phase is phase_before
    assert session.state_version == version_before


def test_invalid_event_is_rejected_before_ingest() -> None:
    conversion = convert_pause()
    invalid_event = replace(
        conversion.event,
        actor="principal-other",
    )
    invalid = CommandEventConversion(
        event=invalid_event,
        operation_link=conversion.operation_link,
    )
    event_store = RecordingEventStore()
    ingress = SessionControlEventIngress(event_store, ActorGameRuntimeIngress())

    with pytest.raises(DMCommandEventSchemaError):
        run(ingress.ingest(invalid))

    assert event_store.events == []


def test_audit_link_contains_only_allowlisted_correlation_fields() -> None:
    conversion = convert_pause()

    assert conversion.operation_link.audit_fields(
        SessionCommandType.PAUSE_GAME
    ) == {
        "domain": "GAME",
        "command_type": "PAUSE_GAME",
        "event_id": "event-command-1",
        "correlation_id": "correlation-pause_game",
    }
