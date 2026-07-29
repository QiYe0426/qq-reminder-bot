"""Session Control Operation domain model for P3-D-6.2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from game_runtime.session_control.commands import SessionCommandType


class ControlOperationStatus(str, Enum):
    CREATED = "CREATED"
    EXECUTING = "EXECUTING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"


_CONTROL_OPERATION_TRANSITIONS: dict[
    ControlOperationStatus, frozenset[ControlOperationStatus]
] = {
    ControlOperationStatus.CREATED: frozenset(
        {
            ControlOperationStatus.EXECUTING,
            ControlOperationStatus.CANCELLED,
        }
    ),
    ControlOperationStatus.EXECUTING: frozenset(
        {
            ControlOperationStatus.SUCCESS,
            ControlOperationStatus.FAILED,
            ControlOperationStatus.UNKNOWN,
        }
    ),
    ControlOperationStatus.SUCCESS: frozenset(),
    ControlOperationStatus.FAILED: frozenset(),
    ControlOperationStatus.UNKNOWN: frozenset(),
    ControlOperationStatus.CANCELLED: frozenset(),
}


class ControlOperationError(ValueError):
    """Base error for an invalid Control Operation."""


class InvalidControlOperationTransition(ControlOperationError):
    """Raised when an Operation status transition is not frozen."""


@dataclass(slots=True)
class ControlOperation:
    """Persistent intent and result evidence for one Session Command."""

    operation_id: str
    command_id: str
    command_type: SessionCommandType
    game_id: str
    session_id: str
    group_id: str
    requester: str
    binding_version: int | None
    payload_fingerprint: str
    confirmation_reference: str | None
    input_event_id: str | None
    result_event_id: str | None
    observed_state_version: int | None
    result_state_version: int | None
    created_at: datetime
    updated_at: datetime
    status: ControlOperationStatus = ControlOperationStatus.CREATED
    claim_id: str | None = None
    claimed_at: datetime | None = None
    result_code: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "operation_id",
            "command_id",
            "game_id",
            "session_id",
            "group_id",
            "requester",
        ):
            _require_text(name, getattr(self, name))
        if not isinstance(self.command_type, SessionCommandType):
            raise TypeError("command_type must be a SessionCommandType")
        if not isinstance(self.status, ControlOperationStatus):
            raise TypeError("status must be a ControlOperationStatus")
        _require_optional_non_negative("binding_version", self.binding_version)
        _require_fingerprint(self.payload_fingerprint)
        for name in (
            "confirmation_reference",
            "input_event_id",
            "result_event_id",
            "claim_id",
            "result_code",
        ):
            _require_optional_text(name, getattr(self, name))
        _require_optional_non_negative(
            "observed_state_version", self.observed_state_version
        )
        _require_optional_non_negative(
            "result_state_version", self.result_state_version
        )
        _require_time("created_at", self.created_at)
        _require_time("updated_at", self.updated_at)
        _require_optional_time("claimed_at", self.claimed_at)
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")
        if self.claimed_at is not None and self.claimed_at < self.created_at:
            raise ValueError("claimed_at must not be earlier than created_at")
        if (self.claim_id is None) is not (self.claimed_at is None):
            raise ValueError("claim_id and claimed_at must be set together")
        self._validate_status_evidence()

    @property
    def is_terminal(self) -> bool:
        return not _CONTROL_OPERATION_TRANSITIONS[self.status]

    def idempotency_signature(self) -> tuple[object, ...]:
        """Return immutable Command evidence; operation_id is intentionally absent."""

        return (
            self.command_id,
            self.command_type,
            self.game_id,
            self.session_id,
            self.group_id,
            self.requester,
            self.binding_version,
            self.payload_fingerprint,
            self.observed_state_version,
        )

    def bind_input_event(self, event_id: str, *, occurred_at: datetime) -> None:
        """Bind the Event created after the Operation without applying State."""

        _require_text("event_id", event_id)
        self._validate_monotonic_time(occurred_at)
        if self.status is not ControlOperationStatus.CREATED:
            raise InvalidControlOperationTransition(
                "input Event can only be bound while Operation is CREATED"
            )
        if self.input_event_id is not None and self.input_event_id != event_id:
            raise ControlOperationError("input Event is already bound")
        self.input_event_id = event_id
        self.updated_at = occurred_at

    def claim(self, claim_id: str, *, claimed_at: datetime) -> None:
        """CAS transition from CREATED to EXECUTING."""

        _require_text("claim_id", claim_id)
        self._validate_monotonic_time(claimed_at)
        if self.input_event_id is None:
            raise ControlOperationError("Operation requires input_event_id before claim")
        self._require_transition(ControlOperationStatus.EXECUTING)
        self.status = ControlOperationStatus.EXECUTING
        self.claim_id = claim_id
        self.claimed_at = claimed_at
        self.updated_at = claimed_at

    def complete(
        self,
        target: ControlOperationStatus,
        *,
        occurred_at: datetime,
        result_code: str,
        result_event_id: str | None = None,
        result_state_version: int | None = None,
    ) -> None:
        """Close an EXECUTING Operation without applying Session State."""

        if target not in {
            ControlOperationStatus.SUCCESS,
            ControlOperationStatus.FAILED,
            ControlOperationStatus.UNKNOWN,
        }:
            raise InvalidControlOperationTransition(
                "completion target must be SUCCESS, FAILED, or UNKNOWN"
            )
        self._require_transition(target)
        self._validate_monotonic_time(occurred_at)
        _require_text("result_code", result_code)
        _require_optional_text("result_event_id", result_event_id)
        _require_optional_non_negative(
            "result_state_version", result_state_version
        )
        if target in {
            ControlOperationStatus.SUCCESS,
            ControlOperationStatus.FAILED,
        } and (result_event_id is None or result_state_version is None):
            raise ControlOperationError(
                "SUCCESS/FAILED requires result Event and State version evidence"
            )
        if target is ControlOperationStatus.UNKNOWN and (
            result_event_id is not None or result_state_version is not None
        ):
            raise ControlOperationError(
                "UNKNOWN cannot claim committed result evidence"
            )
        self.status = target
        self.result_event_id = result_event_id
        self.result_state_version = result_state_version
        self.result_code = result_code
        self.updated_at = occurred_at

    def cancel(self, *, occurred_at: datetime, result_code: str) -> None:
        """Cancel a CREATED Operation before an Actor claim."""

        self._require_transition(ControlOperationStatus.CANCELLED)
        self._validate_monotonic_time(occurred_at)
        _require_text("result_code", result_code)
        self.status = ControlOperationStatus.CANCELLED
        self.result_code = result_code
        self.updated_at = occurred_at

    def _require_transition(self, target: ControlOperationStatus) -> None:
        if not isinstance(target, ControlOperationStatus):
            raise TypeError("target must be a ControlOperationStatus")
        if target not in _CONTROL_OPERATION_TRANSITIONS[self.status]:
            raise InvalidControlOperationTransition(
                f"control operation transition {self.status.value} -> "
                f"{target.value} is not allowed"
            )

    def _validate_monotonic_time(self, value: datetime) -> None:
        _require_time("occurred_at", value)
        if value < self.updated_at:
            raise ValueError("occurred_at must not move updated_at backwards")

    def _validate_status_evidence(self) -> None:
        if self.status is ControlOperationStatus.CREATED:
            if (
                self.claim_id is not None
                or self.result_code is not None
                or self.result_event_id is not None
                or self.result_state_version is not None
            ):
                raise ValueError("CREATED Operation cannot contain claim/result evidence")
            return
        if self.status is ControlOperationStatus.CANCELLED:
            if self.claim_id is not None:
                raise ValueError("CANCELLED Operation cannot contain claim evidence")
            if self.result_code is None:
                raise ValueError("CANCELLED Operation requires result_code")
            if (
                self.result_event_id is not None
                or self.result_state_version is not None
            ):
                raise ValueError("CANCELLED Operation cannot contain result Event evidence")
            return
        if self.claim_id is None:
            raise ValueError(f"{self.status.value} Operation requires claim evidence")
        if self.status is ControlOperationStatus.EXECUTING:
            if (
                self.result_code is not None
                or self.result_event_id is not None
                or self.result_state_version is not None
            ):
                raise ValueError("EXECUTING Operation cannot contain result evidence")
            return
        if self.result_code is None:
            raise ValueError(f"{self.status.value} Operation requires result_code")
        if self.status in {
            ControlOperationStatus.SUCCESS,
            ControlOperationStatus.FAILED,
        } and (self.result_event_id is None or self.result_state_version is None):
            raise ValueError(
                f"{self.status.value} Operation requires committed result evidence"
            )
        if self.status is ControlOperationStatus.UNKNOWN and (
            self.result_event_id is not None or self.result_state_version is not None
        ):
            raise ValueError("UNKNOWN Operation cannot contain committed result evidence")


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} must not be empty")


def _require_optional_text(name: str, value: object) -> None:
    if value is not None:
        _require_text(name, value)


def _require_optional_non_negative(name: str, value: object) -> None:
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer or None")
    if value < 0:
        raise ValueError(f"{name} must not be negative")


def _require_fingerprint(value: object) -> None:
    _require_text("payload_fingerprint", value)
    assert isinstance(value, str)
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError("payload_fingerprint must be lowercase SHA-256 hex")


def _require_time(name: str, value: object) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _require_optional_time(name: str, value: object) -> None:
    if value is not None:
        _require_time(name, value)
