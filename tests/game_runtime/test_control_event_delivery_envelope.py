from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone
from importlib import import_module

import pytest

from game_runtime.event import EventVisibility, GameEvent, GameEventSource, GameEventType
from game_runtime.session_control import (
    ControlOperation,
    ControlOperationStatus,
    DMCommandEventPayload,
    SessionCommandType,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def delivery_module():
    return import_module("game_runtime.session_control.delivery")


def envelope_type():
    return delivery_module().ControlEventDeliveryEnvelope


def envelope_error_type():
    return delivery_module().ControlEventDeliveryEnvelopeError


def make_event(
    *,
    event_type: GameEventType = GameEventType.DM_COMMAND,
    source: GameEventSource = GameEventSource.CONTROL,
    visibility: EventVisibility = EventVisibility.DM_CONTROL,
    command_type: SessionCommandType = SessionCommandType.PAUSE_GAME,
    command_id: str = "command-1",
    requester: str = "principal-dm-1",
    observed_state_version: int = 4,
    correlation_id: str = "correlation-1",
) -> GameEvent:
    payload = DMCommandEventPayload(
        command_id=command_id,
        command_type=command_type,
        requester=requester,
        causation_event_id="message-1",
        observed_state_version=observed_state_version,
        payload_reference="sha256:" + "a" * 64,
        payload_fingerprint="a" * 64,
    )
    return GameEvent(
        event_id="event-command-1",
        game_id="game-1",
        session_id="session-1",
        event_type=event_type,
        actor=requester,
        source=source,
        correlation_id=correlation_id,
        timestamp=NOW,
        payload=payload.to_mapping(),
        visibility=visibility,
        observed_state_version=observed_state_version,
        causation_event_id="message-1",
    )


def make_operation(
    *,
    command_type: SessionCommandType = SessionCommandType.PAUSE_GAME,
    command_id: str = "command-1",
    game_id: str = "game-1",
    session_id: str = "session-1",
    requester: str = "principal-dm-1",
    binding_version: int | None = 2,
    observed_state_version: int | None = 4,
    input_event_id: str | None = "event-command-1",
) -> ControlOperation:
    return ControlOperation(
        operation_id="operation-1",
        command_id=command_id,
        command_type=command_type,
        game_id=game_id,
        session_id=session_id,
        group_id="group-1",
        requester=requester,
        binding_version=binding_version,
        payload_fingerprint="a" * 64,
        confirmation_reference="confirmation-1",
        input_event_id=input_event_id,
        result_event_id=None,
        observed_state_version=observed_state_version,
        result_state_version=None,
        created_at=NOW,
        updated_at=NOW,
        status=ControlOperationStatus.CREATED,
    )


def make_envelope(**overrides):
    values = {
        "event": make_event(),
        "event_sequence_no": 7,
        "operation_id": "operation-1",
        "command_id": "command-1",
        "observed_state_version": 4,
        "requester_principal_ref": "principal-dm-1",
        "requester_binding_version": 2,
        "authorization_reference": "authorization-1",
        "confirmation_reference": "confirmation-1",
        "correlation_id": "correlation-1",
        "stored_event_reference": "event-command-1",
    }
    values.update(overrides)
    return envelope_type()(**values)


def test_creates_envelope_from_persisted_operation_evidence() -> None:
    envelope = envelope_type().from_persisted_evidence(
        event=make_event(),
        event_sequence_no=7,
        operation=make_operation(),
        authorization_reference="authorization-1",
        stored_event_reference="event-command-1",
    )

    assert envelope.event.event_id == "event-command-1"
    assert envelope.event_sequence_no == 7
    assert envelope.operation_id == "operation-1"
    assert envelope.requester_binding_version == 2
    assert envelope.confirmation_reference == "confirmation-1"


def test_envelope_is_immutable() -> None:
    envelope = make_envelope()

    with pytest.raises(FrozenInstanceError):
        envelope.command_id = "command-other"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("event_type", "source", "visibility"),
    [
        (GameEventType.MESSAGE_RECEIVED, GameEventSource.CONTROL, EventVisibility.DM_CONTROL),
        (GameEventType.DM_COMMAND, GameEventSource.PLATFORM, EventVisibility.DM_CONTROL),
        (GameEventType.DM_COMMAND, GameEventSource.CONTROL, EventVisibility.SYSTEM_ONLY),
    ],
)
def test_rejects_wrong_event_type_source_or_visibility(
    event_type: GameEventType,
    source: GameEventSource,
    visibility: EventVisibility,
) -> None:
    with pytest.raises(envelope_error_type()):
        make_envelope(event=make_event(event_type=event_type, source=source, visibility=visibility))


def test_rejects_command_binding_mismatch() -> None:
    with pytest.raises(envelope_error_type(), match="command"):
        envelope_type().from_persisted_evidence(
            event=make_event(),
            event_sequence_no=7,
            operation=make_operation(command_id="command-other"),
            authorization_reference="authorization-1",
            stored_event_reference="event-command-1",
        )


@pytest.mark.parametrize(
    "operation",
    [
        make_operation(game_id="game-other"),
        make_operation(session_id="session-other"),
    ],
)
def test_rejects_operation_game_or_session_mismatch(
    operation: ControlOperation,
) -> None:
    with pytest.raises(envelope_error_type(), match="scope"):
        envelope_type().from_persisted_evidence(
            event=make_event(),
            event_sequence_no=7,
            operation=operation,
            authorization_reference="authorization-1",
            stored_event_reference="event-command-1",
        )


def test_rejects_requester_mismatch() -> None:
    with pytest.raises(envelope_error_type(), match="requester"):
        make_envelope(requester_principal_ref="principal-other")


def test_rejects_version_mismatch() -> None:
    with pytest.raises(envelope_error_type(), match="version"):
        make_envelope(observed_state_version=5)


def test_rejects_correlation_mismatch() -> None:
    with pytest.raises(envelope_error_type(), match="correlation"):
        make_envelope(correlation_id="correlation-other")


@pytest.mark.parametrize("sequence", [0, -1, True])
def test_rejects_invalid_sequence(sequence: object) -> None:
    with pytest.raises(envelope_error_type(), match="sequence"):
        make_envelope(event_sequence_no=sequence)


def test_rejects_stored_event_reference_mismatch() -> None:
    with pytest.raises(envelope_error_type(), match="stored_event_reference"):
        make_envelope(stored_event_reference="event-other")


def test_rejects_create_session_from_regular_delivery_path() -> None:
    event = make_event(command_type=SessionCommandType.CREATE_SESSION)
    operation = make_operation(command_type=SessionCommandType.CREATE_SESSION)

    with pytest.raises(envelope_error_type(), match="CREATE_SESSION"):
        envelope_type().from_persisted_evidence(
            event=event,
            event_sequence_no=7,
            operation=operation,
            authorization_reference="authorization-1",
            stored_event_reference="event-command-1",
        )


def test_contract_contains_only_delivery_evidence() -> None:
    expected_fields = {
        "event",
        "event_sequence_no",
        "operation_id",
        "command_id",
        "observed_state_version",
        "requester_principal_ref",
        "requester_binding_version",
        "authorization_reference",
        "confirmation_reference",
        "correlation_id",
        "stored_event_reference",
    }

    assert {item.name for item in fields(envelope_type())} == expected_fields
    envelope = make_envelope()
    assert not hasattr(envelope, "__dict__")
    for forbidden in (
        "snapshot",
        "state",
        "apply_plan",
        "repository",
        "port",
        "callback",
        "operation_claim_id",
    ):
        assert not hasattr(envelope, forbidden)


def test_contract_is_exported_from_session_control_package() -> None:
    package = import_module("game_runtime.session_control")

    assert package.ControlEventDeliveryEnvelope is envelope_type()
