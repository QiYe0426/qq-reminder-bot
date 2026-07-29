"""Governed Session Command to DM_COMMAND Event integration for P3-D-5."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from game_runtime.event import (
    EventVisibility,
    GameEvent,
    GameEventSource,
    GameEventType,
)
from game_runtime.session_control.authorization import (
    AuthorizationDecision,
    AuthorizationResult,
)
from game_runtime.session_control.commands import SessionCommand, SessionCommandType
from game_runtime.session_control.confirmation import (
    CommandConfirmation,
    ConfirmationPolicyContext,
    ConfirmationRequirement,
    ConfirmationStatus,
    confirmation_requirement_for,
    fingerprint_payload,
)


ControlEventVisibility = EventVisibility


class CommandEventIntegrationError(ValueError):
    """Base error for a rejected Command-to-Event integration."""


class CommandGovernanceRejected(CommandEventIntegrationError):
    """Raised before Event creation when governance evidence is insufficient."""


class DMCommandEventSchemaError(CommandEventIntegrationError):
    """Raised when a DM_COMMAND Event does not match its controlled schema."""


class EventIngestError(CommandEventIntegrationError):
    """Raised when a persisted Event cannot be delivered to its mailbox."""


@dataclass(frozen=True, slots=True)
class DMCommandEventPayload:
    command_id: str
    command_type: SessionCommandType
    requester: str
    causation_event_id: str
    observed_state_version: int
    payload_reference: str
    payload_fingerprint: str
    visibility: EventVisibility = EventVisibility.DM_CONTROL

    def __post_init__(self) -> None:
        for name, value in (
            ("command_id", self.command_id),
            ("requester", self.requester),
            ("causation_event_id", self.causation_event_id),
            ("payload_reference", self.payload_reference),
            ("payload_fingerprint", self.payload_fingerprint),
        ):
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a string")
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        if not isinstance(self.command_type, SessionCommandType):
            raise TypeError("command_type must be a SessionCommandType")
        if not isinstance(self.observed_state_version, int) or isinstance(
            self.observed_state_version, bool
        ):
            raise TypeError("observed_state_version must be an integer")
        if self.observed_state_version < 0:
            raise ValueError("observed_state_version must not be negative")
        if self.visibility is not EventVisibility.DM_CONTROL:
            raise ValueError("DM_COMMAND visibility must be DM_CONTROL")
        if len(self.payload_fingerprint) != 64 or any(
            character not in "0123456789abcdef"
            for character in self.payload_fingerprint
        ):
            raise ValueError("payload_fingerprint must be lowercase SHA-256 hex")
        expected_reference = f"sha256:{self.payload_fingerprint}"
        if self.payload_reference != expected_reference:
            raise ValueError("payload_reference must bind payload_fingerprint")

    def to_mapping(self) -> Mapping[str, object]:
        return {
            "command_id": self.command_id,
            "command_type": self.command_type.value,
            "requester": self.requester,
            "causation_event_id": self.causation_event_id,
            "observed_state_version": self.observed_state_version,
            "payload_reference": self.payload_reference,
            "payload_fingerprint": self.payload_fingerprint,
            "visibility": self.visibility.value,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> DMCommandEventPayload:
        if not isinstance(payload, Mapping):
            raise DMCommandEventSchemaError("event payload must be a Mapping")
        expected_fields = {
            "command_id",
            "command_type",
            "requester",
            "causation_event_id",
            "observed_state_version",
            "payload_reference",
            "payload_fingerprint",
            "visibility",
        }
        if set(payload) != expected_fields:
            raise DMCommandEventSchemaError(
                "DM_COMMAND payload fields do not match the schema"
            )
        try:
            return cls(
                command_id=payload["command_id"],  # type: ignore[arg-type]
                command_type=SessionCommandType(payload["command_type"]),
                requester=payload["requester"],  # type: ignore[arg-type]
                causation_event_id=payload["causation_event_id"],  # type: ignore[arg-type]
                observed_state_version=payload["observed_state_version"],  # type: ignore[arg-type]
                payload_reference=payload["payload_reference"],  # type: ignore[arg-type]
                payload_fingerprint=payload["payload_fingerprint"],  # type: ignore[arg-type]
                visibility=EventVisibility(payload["visibility"]),
            )
        except (TypeError, ValueError) as exc:
            raise DMCommandEventSchemaError(
                "DM_COMMAND payload contains invalid values"
            ) from exc


@dataclass(frozen=True, slots=True)
class ControlOperationLink:
    """Privacy-safe correlation retained across Command, Operation and Event."""

    command_id: str
    event_id: str
    correlation_id: str

    def __post_init__(self) -> None:
        for name, value in (
            ("command_id", self.command_id),
            ("event_id", self.event_id),
            ("correlation_id", self.correlation_id),
        ):
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a string")
            if not value.strip():
                raise ValueError(f"{name} must not be empty")

    def audit_fields(self, command_type: SessionCommandType) -> Mapping[str, str]:
        """Return the allowlisted GAME-domain Audit correlation fields."""

        if not isinstance(command_type, SessionCommandType):
            raise TypeError("command_type must be a SessionCommandType")
        return {
            "domain": "GAME",
            "command_type": command_type.value,
            "event_id": self.event_id,
            "correlation_id": self.correlation_id,
        }


@dataclass(frozen=True, slots=True)
class CommandEventConversion:
    event: GameEvent
    operation_link: ControlOperationLink


class EventIngestPort(Protocol):
    async def append_event(self, event: GameEvent) -> object:
        """Persist one Event in the existing Event Store pipeline."""


class EventMailboxPort(Protocol):
    def accept(self, event: GameEvent) -> bool:
        """Notify the Session mailbox after successful Event ingest."""


@dataclass(frozen=True, slots=True)
class EventIngestResult:
    conversion: CommandEventConversion
    stored_record: object
    delivered: bool


class SessionCommandEventConverter:
    """Convert a governed command into the existing GameEvent envelope."""

    def convert(
        self,
        command: SessionCommand,
        authorization: AuthorizationResult,
        *,
        event_id: str,
        causation_event_id: str,
        confirmation: CommandConfirmation | None = None,
        confirmation_context: ConfirmationPolicyContext | None = None,
    ) -> CommandEventConversion:
        if not isinstance(command, SessionCommand):
            raise TypeError("command must be a SessionCommand")
        if not isinstance(authorization, AuthorizationResult):
            raise TypeError("authorization must be an AuthorizationResult")
        if authorization.decision is AuthorizationDecision.DENY:
            raise CommandGovernanceRejected("authorization denied command")
        if authorization.required_permission.value != command.command_type.value:
            raise CommandGovernanceRejected(
                "authorization permission does not match command"
            )

        requirement = confirmation_requirement_for(
            command,
            confirmation_context,
        )
        requires_confirmation = (
            requirement is ConfirmationRequirement.REQUIRED
            or authorization.decision is AuthorizationDecision.REQUIRE_CONFIRMATION
        )
        if requires_confirmation and not _confirmed_for_command(
            confirmation,
            command,
        ):
            raise CommandGovernanceRejected(
                "required confirmation has not been validated"
            )

        if command.game_id is None or command.session_id is None:
            raise DMCommandEventSchemaError(
                "DM_COMMAND Event requires game_id and session_id"
            )
        if command.observed_state_version is None:
            raise DMCommandEventSchemaError(
                "DM_COMMAND Event requires observed_state_version"
            )
        for name, value in (
            ("event_id", event_id),
            ("causation_event_id", causation_event_id),
        ):
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a string")
            if not value.strip():
                raise DMCommandEventSchemaError(f"{name} must not be empty")

        payload_fingerprint = fingerprint_payload(command.payload)
        payload = DMCommandEventPayload(
            command_id=command.command_id,
            command_type=command.command_type,
            requester=command.requester,
            causation_event_id=causation_event_id,
            observed_state_version=command.observed_state_version,
            payload_reference=f"sha256:{payload_fingerprint}",
            payload_fingerprint=payload_fingerprint,
        )
        event = GameEvent(
            event_id=event_id,
            game_id=command.game_id,
            session_id=command.session_id,
            event_type=GameEventType.DM_COMMAND,
            actor=command.requester,
            source=GameEventSource.CONTROL,
            correlation_id=command.correlation_id,
            timestamp=command.requested_at,
            payload=payload.to_mapping(),
            visibility=EventVisibility.DM_CONTROL,
            observed_state_version=command.observed_state_version,
            causation_event_id=causation_event_id,
        )
        validate_dm_command_event(event)
        return CommandEventConversion(
            event=event,
            operation_link=ControlOperationLink(
                command_id=command.command_id,
                event_id=event.event_id,
                correlation_id=command.correlation_id,
            ),
        )


class SessionControlEventIngress:
    """Persist a validated Event, then notify the existing Session mailbox."""

    def __init__(
        self,
        event_store: EventIngestPort,
        mailbox: EventMailboxPort,
    ) -> None:
        self._event_store = event_store
        self._mailbox = mailbox

    async def ingest(
        self,
        conversion: CommandEventConversion,
    ) -> EventIngestResult:
        if not isinstance(conversion, CommandEventConversion):
            raise TypeError("conversion must be a CommandEventConversion")
        payload = validate_dm_command_event(conversion.event)
        link = conversion.operation_link
        if (
            link.command_id != payload.command_id
            or link.event_id != conversion.event.event_id
            or link.correlation_id != conversion.event.correlation_id
        ):
            raise DMCommandEventSchemaError(
                "Operation link does not match the DM_COMMAND Event"
            )
        stored_record = await self._event_store.append_event(conversion.event)
        delivered = self._mailbox.accept(conversion.event)
        if not delivered:
            raise EventIngestError(
                "persisted Event was not accepted by the Session mailbox"
            )
        return EventIngestResult(
            conversion=conversion,
            stored_record=stored_record,
            delivered=True,
        )


def validate_dm_command_event(event: GameEvent) -> DMCommandEventPayload:
    """Validate the controlled schema without modifying the Event."""

    if not isinstance(event, GameEvent):
        raise TypeError("event must be a GameEvent")
    if event.event_type is not GameEventType.DM_COMMAND:
        raise DMCommandEventSchemaError("event_type must be DM_COMMAND")
    if event.source is not GameEventSource.CONTROL:
        raise DMCommandEventSchemaError("DM_COMMAND source must be CONTROL")
    payload = DMCommandEventPayload.from_mapping(event.payload)
    if event.actor != payload.requester:
        raise DMCommandEventSchemaError("event actor must match requester")
    if event.visibility is not EventVisibility.DM_CONTROL:
        raise DMCommandEventSchemaError("DM_COMMAND visibility must be DM_CONTROL")
    if event.observed_state_version != payload.observed_state_version:
        raise DMCommandEventSchemaError(
            "Event observed_state_version must match payload"
        )
    if event.causation_event_id != payload.causation_event_id:
        raise DMCommandEventSchemaError("Event causation_event_id must match payload")
    return payload


def _confirmed_for_command(
    confirmation: CommandConfirmation | None,
    command: SessionCommand,
) -> bool:
    if (
        confirmation is None
        or confirmation.status is not ConfirmationStatus.CONFIRMED
    ):
        return False
    return all(
        (
            confirmation.command_id == command.command_id,
            confirmation.command_type is command.command_type,
            confirmation.game_id == command.game_id,
            confirmation.session_id == command.session_id,
            confirmation.requester == command.requester,
            confirmation.observed_state_version == command.observed_state_version,
            confirmation.payload_fingerprint == fingerprint_payload(command.payload),
        )
    )
