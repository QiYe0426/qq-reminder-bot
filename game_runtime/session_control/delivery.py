"""Immutable delivery evidence for persisted Session Control Events."""

from __future__ import annotations

from dataclasses import dataclass

from game_runtime.event import GameEvent
from game_runtime.session_control.commands import SessionCommandType
from game_runtime.session_control.event_integration import (
    DMCommandEventSchemaError,
    validate_dm_command_event,
)
from game_runtime.session_control.operation import (
    ControlOperation,
    ControlOperationStatus,
)


class ControlEventDeliveryEnvelopeError(ValueError):
    """Raised when persisted delivery evidence is internally inconsistent."""


@dataclass(frozen=True, slots=True)
class ControlEventDeliveryEnvelope:
    """Transport-only evidence for delivering one persisted DM_COMMAND Event."""

    event: GameEvent
    event_sequence_no: int
    operation_id: str
    command_id: str
    observed_state_version: int
    requester_principal_ref: str
    requester_binding_version: int
    authorization_reference: str
    confirmation_reference: str | None
    correlation_id: str
    stored_event_reference: str

    def __post_init__(self) -> None:
        try:
            payload = validate_dm_command_event(self.event)
        except (TypeError, DMCommandEventSchemaError) as exc:
            raise ControlEventDeliveryEnvelopeError(
                "event must be a valid controlled DM_COMMAND Event"
            ) from exc

        if payload.command_type is SessionCommandType.CREATE_SESSION:
            raise ControlEventDeliveryEnvelopeError(
                "CREATE_SESSION must use the bootstrap delivery path"
            )

        _require_positive_int("event_sequence_no", self.event_sequence_no)
        _require_non_negative_int(
            "observed_state_version", self.observed_state_version
        )
        _require_non_negative_int(
            "requester_binding_version", self.requester_binding_version
        )
        for name, value in (
            ("operation_id", self.operation_id),
            ("command_id", self.command_id),
            ("requester_principal_ref", self.requester_principal_ref),
            ("authorization_reference", self.authorization_reference),
            ("correlation_id", self.correlation_id),
            ("stored_event_reference", self.stored_event_reference),
        ):
            _require_text(name, value)
        if self.confirmation_reference is not None:
            _require_text("confirmation_reference", self.confirmation_reference)

        if self.command_id != payload.command_id:
            raise ControlEventDeliveryEnvelopeError(
                "command_id must match the DM_COMMAND payload"
            )
        if (
            self.requester_principal_ref != self.event.actor
            or self.requester_principal_ref != payload.requester
        ):
            raise ControlEventDeliveryEnvelopeError(
                "requester_principal_ref must match Event requester evidence"
            )
        if (
            self.observed_state_version != self.event.observed_state_version
            or self.observed_state_version != payload.observed_state_version
        ):
            raise ControlEventDeliveryEnvelopeError(
                "observed_state_version must match Event version evidence"
            )
        if self.correlation_id != self.event.correlation_id:
            raise ControlEventDeliveryEnvelopeError(
                "correlation_id must match the Event correlation"
            )
        if self.stored_event_reference != self.event.event_id:
            raise ControlEventDeliveryEnvelopeError(
                "stored_event_reference must equal event_id"
            )

    @classmethod
    def from_persisted_evidence(
        cls,
        *,
        event: GameEvent,
        event_sequence_no: int,
        operation: ControlOperation,
        authorization_reference: str,
        stored_event_reference: str,
    ) -> ControlEventDeliveryEnvelope:
        """Build an Envelope only from matching persisted Event/Operation evidence."""

        if not isinstance(operation, ControlOperation):
            raise ControlEventDeliveryEnvelopeError(
                "operation must be a ControlOperation"
            )
        if operation.status is not ControlOperationStatus.CREATED:
            raise ControlEventDeliveryEnvelopeError(
                "delivery requires an unclaimed CREATED operation"
            )

        try:
            payload = validate_dm_command_event(event)
        except (TypeError, DMCommandEventSchemaError) as exc:
            raise ControlEventDeliveryEnvelopeError(
                "event must be a valid controlled DM_COMMAND Event"
            ) from exc

        if payload.command_type is SessionCommandType.CREATE_SESSION:
            raise ControlEventDeliveryEnvelopeError(
                "CREATE_SESSION must use the bootstrap delivery path"
            )
        if operation.game_id != event.game_id or operation.session_id != event.session_id:
            raise ControlEventDeliveryEnvelopeError(
                "operation scope must match Event game_id and session_id"
            )
        if (
            operation.command_id != payload.command_id
            or operation.command_type is not payload.command_type
        ):
            raise ControlEventDeliveryEnvelopeError(
                "operation command evidence must match the DM_COMMAND Event"
            )
        if operation.input_event_id != event.event_id:
            raise ControlEventDeliveryEnvelopeError(
                "operation input Event must match stored Event evidence"
            )
        if operation.requester != event.actor or operation.requester != payload.requester:
            raise ControlEventDeliveryEnvelopeError(
                "operation requester must match Event requester evidence"
            )
        if operation.binding_version is None:
            raise ControlEventDeliveryEnvelopeError(
                "operation requester binding version is required"
            )
        if (
            operation.observed_state_version != event.observed_state_version
            or operation.observed_state_version != payload.observed_state_version
        ):
            raise ControlEventDeliveryEnvelopeError(
                "operation observed state version must match Event version evidence"
            )
        if operation.payload_fingerprint != payload.payload_fingerprint:
            raise ControlEventDeliveryEnvelopeError(
                "operation command payload fingerprint must match Event evidence"
            )

        return cls(
            event=event,
            event_sequence_no=event_sequence_no,
            operation_id=operation.operation_id,
            command_id=operation.command_id,
            observed_state_version=operation.observed_state_version,
            requester_principal_ref=operation.requester,
            requester_binding_version=operation.binding_version,
            authorization_reference=authorization_reference,
            confirmation_reference=operation.confirmation_reference,
            correlation_id=event.correlation_id,
            stored_event_reference=stored_event_reference,
        )


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ControlEventDeliveryEnvelopeError(f"{name} must be non-empty text")


def _require_non_negative_int(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ControlEventDeliveryEnvelopeError(
            f"{name} must be a non-negative integer"
        )


def _require_positive_int(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ControlEventDeliveryEnvelopeError(
            f"{name} sequence must be a positive integer"
        )
