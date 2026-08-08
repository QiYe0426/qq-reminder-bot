"""Pure, single-use Session Control confirmation framework for P3-D-4."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
import hashlib
import json
from threading import RLock

from game_runtime.session_control.commands import (
    AssignCharacterPayload,
    ChangePhasePayload,
    SessionCommand,
    SessionCommandPayload,
    SessionCommandType,
    SetScriptPayload,
)


class ConfirmationStatus(str, Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class ConfirmationRequirement(str, Enum):
    REQUIRED = "REQUIRED"
    NOT_REQUIRED = "NOT_REQUIRED"


class ConfirmationInvalidReason(str, Enum):
    EXPIRED = "EXPIRED"
    ALREADY_CONFIRMED = "ALREADY_CONFIRMED"
    CANCELLED = "CANCELLED"
    COMMAND_ID_MISMATCH = "COMMAND_ID_MISMATCH"
    COMMAND_TYPE_MISMATCH = "COMMAND_TYPE_MISMATCH"
    GAME_ID_MISMATCH = "GAME_ID_MISMATCH"
    SESSION_ID_MISMATCH = "SESSION_ID_MISMATCH"
    REQUESTER_MISMATCH = "REQUESTER_MISMATCH"
    STATE_VERSION_MISMATCH = "STATE_VERSION_MISMATCH"
    PAYLOAD_FINGERPRINT_MISMATCH = "PAYLOAD_FINGERPRINT_MISMATCH"


@dataclass(frozen=True, slots=True)
class ConfirmationPolicyContext:
    """Known setup facts used by conditional confirmation rules."""

    script_already_set: bool = False
    character_binding_exists: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.script_already_set, bool):
            raise TypeError("script_already_set must be a boolean")
        if not isinstance(self.character_binding_exists, bool):
            raise TypeError("character_binding_exists must be a boolean")


@dataclass(slots=True)
class CommandConfirmation:
    confirmation_id: str
    command_id: str
    command_type: SessionCommandType
    game_id: str
    session_id: str
    requester: str
    observed_state_version: int
    payload_fingerprint: str
    created_at: datetime
    expires_at: datetime
    status: ConfirmationStatus = ConfirmationStatus.PENDING
    _lock: RLock = field(default_factory=RLock, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name, value in (
            ("confirmation_id", self.confirmation_id),
            ("command_id", self.command_id),
            ("game_id", self.game_id),
            ("session_id", self.session_id),
            ("requester", self.requester),
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
        for name, value in (
            ("created_at", self.created_at),
            ("expires_at", self.expires_at),
        ):
            if not isinstance(value, datetime):
                raise TypeError(f"{name} must be a datetime")
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        if not isinstance(self.status, ConfirmationStatus):
            raise TypeError("status must be a ConfirmationStatus")

    def cancel(self) -> None:
        """Cancel a still-pending confirmation without touching a command."""

        with self._lock:
            if self.status is ConfirmationStatus.PENDING:
                self.status = ConfirmationStatus.CANCELLED


@dataclass(frozen=True, slots=True)
class ConfirmationValidationResult:
    valid: bool
    reason: ConfirmationInvalidReason | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.valid, bool):
            raise TypeError("valid must be a boolean")
        if self.valid and self.reason is not None:
            raise ValueError("valid result cannot contain an invalid reason")
        if not self.valid and not isinstance(
            self.reason, ConfirmationInvalidReason
        ):
            raise ValueError("invalid result requires an invalid reason")


_ALWAYS_CONFIRM = frozenset(
    {
        SessionCommandType.START_GAME,
        SessionCommandType.RESUME_GAME,
        SessionCommandType.END_GAME,
        SessionCommandType.REPLACE_PLAYER,
        SessionCommandType.ACTIVATE_RULE_SET,
        SessionCommandType.REVEAL_CLUE,
        SessionCommandType.ACTIVATE_QUEST,
    }
)


def confirmation_requirement_for(
    command: SessionCommand,
    context: ConfirmationPolicyContext | None = None,
) -> ConfirmationRequirement:
    """Return the frozen confirmation requirement without side effects."""

    if not isinstance(command, SessionCommand):
        raise TypeError("command must be a SessionCommand")
    policy_context = (
        context if context is not None else ConfirmationPolicyContext()
    )
    if not isinstance(policy_context, ConfirmationPolicyContext):
        raise TypeError("context must be a ConfirmationPolicyContext")

    if command.command_type in _ALWAYS_CONFIRM:
        return ConfirmationRequirement.REQUIRED
    if command.command_type is SessionCommandType.SET_SCRIPT:
        _require_payload(command.payload, SetScriptPayload)
        return (
            ConfirmationRequirement.REQUIRED
            if policy_context.script_already_set
            else ConfirmationRequirement.NOT_REQUIRED
        )
    if command.command_type is SessionCommandType.ASSIGN_CHARACTER:
        _require_payload(command.payload, AssignCharacterPayload)
        return (
            ConfirmationRequirement.REQUIRED
            if policy_context.character_binding_exists
            else ConfirmationRequirement.NOT_REQUIRED
        )
    if command.command_type is SessionCommandType.CHANGE_PHASE:
        payload = _require_payload(command.payload, ChangePhasePayload)
        phase_type = type(payload.target_phase)
        return (
            ConfirmationRequirement.REQUIRED
            if payload.target_phase in {phase_type.VOTING, phase_type.ENDING}
            else ConfirmationRequirement.NOT_REQUIRED
        )
    return ConfirmationRequirement.NOT_REQUIRED


def create_confirmation(
    command: SessionCommand,
    *,
    confirmation_id: str,
    created_at: datetime,
    expires_at: datetime,
    context: ConfirmationPolicyContext | None = None,
) -> CommandConfirmation:
    """Create a bound confirmation only when the policy requires one."""

    requirement = confirmation_requirement_for(command, context)
    if requirement is not ConfirmationRequirement.REQUIRED:
        raise ValueError("command does not require confirmation")
    if command.game_id is None or command.session_id is None:
        raise ValueError("confirmation requires game_id and session_id")
    if command.observed_state_version is None:
        raise ValueError("confirmation requires observed_state_version")
    return CommandConfirmation(
        confirmation_id=confirmation_id,
        command_id=command.command_id,
        command_type=command.command_type,
        game_id=command.game_id,
        session_id=command.session_id,
        requester=command.requester,
        observed_state_version=command.observed_state_version,
        payload_fingerprint=fingerprint_payload(command.payload),
        created_at=created_at,
        expires_at=expires_at,
    )


def validate_confirmation(
    confirmation: CommandConfirmation,
    command: SessionCommand,
    *,
    validated_at: datetime,
) -> ConfirmationValidationResult:
    """Atomically validate and consume one confirmation; execute nothing."""

    if not isinstance(confirmation, CommandConfirmation):
        raise TypeError("confirmation must be a CommandConfirmation")
    if not isinstance(command, SessionCommand):
        raise TypeError("command must be a SessionCommand")
    if not isinstance(validated_at, datetime):
        raise TypeError("validated_at must be a datetime")
    if validated_at.tzinfo is None or validated_at.utcoffset() is None:
        raise ValueError("validated_at must be timezone-aware")

    with confirmation._lock:
        status_reason = _status_invalid_reason(confirmation.status)
        if status_reason is not None:
            return ConfirmationValidationResult(False, status_reason)
        if validated_at >= confirmation.expires_at:
            confirmation.status = ConfirmationStatus.EXPIRED
            return ConfirmationValidationResult(
                False,
                ConfirmationInvalidReason.EXPIRED,
            )

        checks = (
            (
                confirmation.command_id == command.command_id,
                ConfirmationInvalidReason.COMMAND_ID_MISMATCH,
            ),
            (
                confirmation.command_type is command.command_type,
                ConfirmationInvalidReason.COMMAND_TYPE_MISMATCH,
            ),
            (
                confirmation.game_id == command.game_id,
                ConfirmationInvalidReason.GAME_ID_MISMATCH,
            ),
            (
                confirmation.session_id == command.session_id,
                ConfirmationInvalidReason.SESSION_ID_MISMATCH,
            ),
            (
                confirmation.requester == command.requester,
                ConfirmationInvalidReason.REQUESTER_MISMATCH,
            ),
            (
                confirmation.observed_state_version
                == command.observed_state_version,
                ConfirmationInvalidReason.STATE_VERSION_MISMATCH,
            ),
            (
                confirmation.payload_fingerprint
                == fingerprint_payload(command.payload),
                ConfirmationInvalidReason.PAYLOAD_FINGERPRINT_MISMATCH,
            ),
        )
        for matches, reason in checks:
            if not matches:
                return ConfirmationValidationResult(False, reason)

        confirmation.status = ConfirmationStatus.CONFIRMED
        return ConfirmationValidationResult(True)


def fingerprint_payload(payload: SessionCommandPayload) -> str:
    """Return a deterministic, schema-bound SHA-256 payload fingerprint."""

    if not isinstance(payload, SessionCommandPayload):
        raise TypeError("payload must be a SessionCommandPayload")
    canonical = {
        "payload_type": type(payload).__name__,
        "fields": _canonical_value(asdict(payload)),
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported payload fingerprint value: {type(value).__name__}")


def _require_payload(
    payload: SessionCommandPayload,
    expected_type: type[SessionCommandPayload],
) -> SessionCommandPayload:
    if type(payload) is not expected_type:
        raise TypeError(f"payload must be {expected_type.__name__}")
    return payload


def _status_invalid_reason(
    status: ConfirmationStatus,
) -> ConfirmationInvalidReason | None:
    reasons = {
        ConfirmationStatus.CONFIRMED: ConfirmationInvalidReason.ALREADY_CONFIRMED,
        ConfirmationStatus.EXPIRED: ConfirmationInvalidReason.EXPIRED,
        ConfirmationStatus.CANCELLED: ConfirmationInvalidReason.CANCELLED,
    }
    return reasons.get(status)
