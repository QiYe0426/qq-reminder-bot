"""Typed schema version 1 payloads for Session Control result Events."""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum
from typing import ClassVar, Mapping, TypeVar

from game_runtime.event.model import EventVisibility, GameEvent, GameEventType
from game_runtime.session import GamePhase, GameSessionStatus


CONTROL_RESULT_SCHEMA_VERSION = 1


class ControlResultPayloadSchemaError(ValueError):
    """Raised when a control result Event does not match its frozen schema."""


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} must not be empty")


def _require_non_negative_int(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must not be negative")


def _require_positive_int(name: str, value: object) -> None:
    _require_non_negative_int(name, value)
    if value == 0:
        raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class ControlResultPayload:
    command_id: str
    operation_id: str
    input_event_id: str
    result_code: str
    result_state_version: int

    enum_fields: ClassVar[Mapping[str, type[Enum]]] = {}

    def __post_init__(self) -> None:
        for name in ("command_id", "operation_id", "input_event_id", "result_code"):
            _require_text(name, getattr(self, name))
        _require_non_negative_int("result_state_version", self.result_state_version)

    def to_mapping(self) -> Mapping[str, object]:
        result: dict[str, object] = {}
        for item in fields(self):
            value = getattr(self, item.name)
            result[item.name] = value.value if isinstance(value, Enum) else value
        return result


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionCreatedPayload(ControlResultPayload):
    status: GameSessionStatus
    phase: GamePhase

    enum_fields: ClassVar[Mapping[str, type[Enum]]] = {
        "status": GameSessionStatus,
        "phase": GamePhase,
    }

    def __post_init__(self) -> None:
        super(SessionCreatedPayload, self).__post_init__()
        if self.status is not GameSessionStatus.CREATED:
            raise ValueError("SESSION_CREATED status must be CREATED")
        if self.phase is not GamePhase.LOBBY:
            raise ValueError("SESSION_CREATED phase must be LOBBY")


@dataclass(frozen=True, slots=True, kw_only=True)
class LifecycleChangedPayload(ControlResultPayload):
    previous_status: GameSessionStatus
    current_status: GameSessionStatus

    enum_fields: ClassVar[Mapping[str, type[Enum]]] = {
        "previous_status": GameSessionStatus,
        "current_status": GameSessionStatus,
    }

    def __post_init__(self) -> None:
        super(LifecycleChangedPayload, self).__post_init__()
        if self.previous_status is self.current_status:
            raise ValueError("lifecycle result must change status")


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionStartedPayload(LifecycleChangedPayload):
    ownership_generation: int

    def __post_init__(self) -> None:
        super(SessionStartedPayload, self).__post_init__()
        if self.current_status is not GameSessionStatus.RUNNING:
            raise ValueError("SESSION_STARTED current_status must be RUNNING")
        _require_positive_int("ownership_generation", self.ownership_generation)


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionPausedPayload(LifecycleChangedPayload):
    reason_code: str

    def __post_init__(self) -> None:
        super(SessionPausedPayload, self).__post_init__()
        if self.current_status is not GameSessionStatus.PAUSED:
            raise ValueError("SESSION_PAUSED current_status must be PAUSED")
        _require_text("reason_code", self.reason_code)


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionResumedPayload(LifecycleChangedPayload):
    ownership_generation: int

    def __post_init__(self) -> None:
        super(SessionResumedPayload, self).__post_init__()
        if self.current_status is not GameSessionStatus.RUNNING:
            raise ValueError("SESSION_RESUMED current_status must be RUNNING")
        _require_positive_int("ownership_generation", self.ownership_generation)


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionEndedPayload(LifecycleChangedPayload):
    retention_reference: str

    def __post_init__(self) -> None:
        super(SessionEndedPayload, self).__post_init__()
        if self.current_status is not GameSessionStatus.ENDED:
            raise ValueError("SESSION_ENDED current_status must be ENDED")
        _require_text("retention_reference", self.retention_reference)


@dataclass(frozen=True, slots=True, kw_only=True)
class ScriptSetPayload(ControlResultPayload):
    script_id: str
    public_name: str
    manifest_reference: str

    def __post_init__(self) -> None:
        super(ScriptSetPayload, self).__post_init__()
        for name in ("script_id", "public_name", "manifest_reference"):
            _require_text(name, getattr(self, name))


@dataclass(frozen=True, slots=True, kw_only=True)
class CharacterAssignedPayload(ControlResultPayload):
    participant_id: str
    character_id: str
    binding_version: int

    def __post_init__(self) -> None:
        super(CharacterAssignedPayload, self).__post_init__()
        _require_text("participant_id", self.participant_id)
        _require_text("character_id", self.character_id)
        _require_positive_int("binding_version", self.binding_version)


@dataclass(frozen=True, slots=True, kw_only=True)
class PlayerReplacedPayload(ControlResultPayload):
    old_participant_id: str
    new_participant_id: str
    binding_version: int

    def __post_init__(self) -> None:
        super(PlayerReplacedPayload, self).__post_init__()
        _require_text("old_participant_id", self.old_participant_id)
        _require_text("new_participant_id", self.new_participant_id)
        if self.old_participant_id == self.new_participant_id:
            raise ValueError("replacement participants must be different")
        _require_positive_int("binding_version", self.binding_version)


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleSetActivatedPayload(ControlResultPayload):
    manifest_reference: str
    rule_set_domain_version: int
    hidden_state_domain_version: int

    def __post_init__(self) -> None:
        super(RuleSetActivatedPayload, self).__post_init__()
        _require_text("manifest_reference", self.manifest_reference)
        _require_positive_int(
            "rule_set_domain_version", self.rule_set_domain_version
        )
        _require_positive_int(
            "hidden_state_domain_version", self.hidden_state_domain_version
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ClueRevealedPayload(ControlResultPayload):
    clue_id: str
    public_disclosure_reference: str
    game_rule_domain_version: int

    def __post_init__(self) -> None:
        super(ClueRevealedPayload, self).__post_init__()
        _require_text("clue_id", self.clue_id)
        _require_text(
            "public_disclosure_reference", self.public_disclosure_reference
        )
        _require_positive_int(
            "game_rule_domain_version", self.game_rule_domain_version
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionControlRejectedPayload(ControlResultPayload):
    reason_code: str
    state_version: int

    def __post_init__(self) -> None:
        super(SessionControlRejectedPayload, self).__post_init__()
        _require_text("reason_code", self.reason_code)
        _require_non_negative_int("state_version", self.state_version)
        if self.state_version != self.result_state_version:
            raise ValueError("state_version must match result_state_version")


@dataclass(frozen=True, slots=True, kw_only=True)
class PhaseChangedPayload(ControlResultPayload):
    previous_phase: GamePhase
    current_phase: GamePhase

    enum_fields: ClassVar[Mapping[str, type[Enum]]] = {
        "previous_phase": GamePhase,
        "current_phase": GamePhase,
    }

    def __post_init__(self) -> None:
        super(PhaseChangedPayload, self).__post_init__()
        if self.previous_phase is self.current_phase:
            raise ValueError("phase result must change phase")


ControlPayloadT = TypeVar("ControlPayloadT", bound=ControlResultPayload)


CONTROL_RESULT_PAYLOAD_TYPES: Mapping[
    GameEventType, type[ControlResultPayload]
] = {
    GameEventType.SESSION_CREATED: SessionCreatedPayload,
    GameEventType.SESSION_STARTED: SessionStartedPayload,
    GameEventType.SESSION_PAUSED: SessionPausedPayload,
    GameEventType.SESSION_RESUMED: SessionResumedPayload,
    GameEventType.SESSION_ENDED: SessionEndedPayload,
    GameEventType.SCRIPT_SET: ScriptSetPayload,
    GameEventType.CHARACTER_ASSIGNED: CharacterAssignedPayload,
    GameEventType.PLAYER_REPLACED: PlayerReplacedPayload,
    GameEventType.RULE_SET_ACTIVATED: RuleSetActivatedPayload,
    GameEventType.CLUE_REVEALED: ClueRevealedPayload,
    GameEventType.SESSION_CONTROL_REJECTED: SessionControlRejectedPayload,
    GameEventType.PHASE_CHANGED: PhaseChangedPayload,
}


CONTROL_RESULT_VISIBILITY: Mapping[GameEventType, EventVisibility] = {
    GameEventType.SESSION_CREATED: EventVisibility.DM_CONTROL,
    GameEventType.SESSION_STARTED: EventVisibility.SYSTEM_ONLY,
    GameEventType.SESSION_PAUSED: EventVisibility.SYSTEM_ONLY,
    GameEventType.SESSION_RESUMED: EventVisibility.SYSTEM_ONLY,
    GameEventType.SESSION_ENDED: EventVisibility.SYSTEM_ONLY,
    GameEventType.SCRIPT_SET: EventVisibility.DM_CONTROL,
    GameEventType.CHARACTER_ASSIGNED: EventVisibility.DM_CONTROL,
    GameEventType.PLAYER_REPLACED: EventVisibility.DM_CONTROL,
    GameEventType.RULE_SET_ACTIVATED: EventVisibility.DM_CONTROL,
    GameEventType.CLUE_REVEALED: EventVisibility.PUBLIC,
    GameEventType.SESSION_CONTROL_REJECTED: EventVisibility.DM_CONTROL,
    GameEventType.PHASE_CHANGED: EventVisibility.PUBLIC,
}


def payload_from_mapping(
    payload_type: type[ControlPayloadT], payload: Mapping[str, object]
) -> ControlPayloadT:
    if not isinstance(payload, Mapping):
        raise ControlResultPayloadSchemaError("control result payload must be a Mapping")
    expected_fields = {item.name for item in fields(payload_type)}
    if set(payload) != expected_fields:
        raise ControlResultPayloadSchemaError(
            "control result payload fields do not match the schema"
        )
    values = dict(payload)
    try:
        for name, enum_type in payload_type.enum_fields.items():
            values[name] = enum_type(values[name])
        return payload_type(**values)
    except (TypeError, ValueError) as exc:
        raise ControlResultPayloadSchemaError(
            "control result payload contains invalid values"
        ) from exc


def validate_control_result_event(event: GameEvent) -> ControlResultPayload:
    """Validate one result Event without applying or persisting it."""

    if not isinstance(event, GameEvent):
        raise TypeError("event must be a GameEvent")
    payload_type = CONTROL_RESULT_PAYLOAD_TYPES.get(event.event_type)
    if payload_type is None:
        raise ControlResultPayloadSchemaError(
            "event_type is not a Session Control result Event"
        )
    if event.schema_version != CONTROL_RESULT_SCHEMA_VERSION:
        raise ControlResultPayloadSchemaError(
            f"unsupported control result schema_version: {event.schema_version}"
        )
    expected_visibility = CONTROL_RESULT_VISIBILITY[event.event_type]
    if event.visibility is not expected_visibility:
        raise ControlResultPayloadSchemaError(
            "control result visibility does not match the Event type"
        )
    if event.observed_state_version is None:
        raise ControlResultPayloadSchemaError(
            "control result Event requires observed_state_version"
        )
    if event.causation_event_id is None:
        raise ControlResultPayloadSchemaError(
            "control result Event requires causation_event_id"
        )
    return payload_from_mapping(payload_type, event.payload)
