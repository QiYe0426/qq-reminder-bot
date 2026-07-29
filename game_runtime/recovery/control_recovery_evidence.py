"""Immutable Control Operation recovery evidence contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import hashlib
import json
from typing import TypeAlias

from game_runtime.event import GameEventType
from game_runtime.persistence.records import EventProcessingStatus
from game_runtime.session_control.operation import ControlOperationStatus


class ControlRecoveryEvidenceError(ValueError):
    """Raised when recovery evidence is incomplete or internally inconsistent."""


class ControlCommitTransactionKind(str, Enum):
    APPLY = "APPLY"
    REJECTION = "REJECTION"


class RecoveryResolutionDisposition(str, Enum):
    COMMITTED_SUCCESS = "COMMITTED_SUCCESS"
    COMMITTED_REJECTION = "COMMITTED_REJECTION"
    NO_COMMIT_CONFIRMED = "NO_COMMIT_CONFIRMED"
    MANUAL_QUARANTINE = "MANUAL_QUARANTINE"


class RecoveryUnknownReason(str, Enum):
    INSUFFICIENT_COMMIT_EVIDENCE = "INSUFFICIENT_COMMIT_EVIDENCE"
    CONSISTENT_READ_UNAVAILABLE = "CONSISTENT_READ_UNAVAILABLE"
    RESOLUTION_REQUIRED = "RESOLUTION_REQUIRED"


class RecoveryFaultReason(str, Enum):
    EVIDENCE_CONTRADICTION = "EVIDENCE_CONTRADICTION"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    SEQUENCE_CONFLICT = "SEQUENCE_CONFLICT"
    COMMIT_EVIDENCE_INVALID = "COMMIT_EVIDENCE_INVALID"


@dataclass(frozen=True, slots=True)
class ControlOperationRecoveryEvidence:
    operation_id: str
    game_id: str
    session_id: str
    command_id: str
    input_event_id: str
    input_sequence_no: int
    claim_id: str | None
    claimed_at: datetime | None
    operation_status: ControlOperationStatus
    operation_updated_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "operation_id",
            "game_id",
            "session_id",
            "command_id",
            "input_event_id",
        ):
            _require_text(name, getattr(self, name))
        _require_positive_int("input_sequence_no", self.input_sequence_no)
        _require_optional_text("claim_id", self.claim_id)
        _require_optional_aware_time("claimed_at", self.claimed_at)
        _require_enum(
            "operation_status", self.operation_status, ControlOperationStatus
        )
        _require_aware_time("operation_updated_at", self.operation_updated_at)
        if self.operation_status in {
            ControlOperationStatus.CREATED,
            ControlOperationStatus.CANCELLED,
        }:
            if self.claim_id is not None or self.claimed_at is not None:
                raise ControlRecoveryEvidenceError(
                    f"{self.operation_status.value} Operation cannot carry a claim"
                )
        elif self.claim_id is None or self.claimed_at is None:
            raise ControlRecoveryEvidenceError(
                f"{self.operation_status.value} Operation requires claim evidence"
            )


@dataclass(frozen=True, slots=True)
class ControlSnapshotRecoveryEvidence:
    snapshot_reference: str
    game_id: str
    session_id: str
    state_version: int
    cursor: int

    def __post_init__(self) -> None:
        for name in ("snapshot_reference", "game_id", "session_id"):
            _require_text(name, getattr(self, name))
        _require_non_negative_int("state_version", self.state_version)
        _require_non_negative_int("cursor", self.cursor)


@dataclass(frozen=True, slots=True)
class ControlInputEventRecoveryEvidence:
    event_id: str
    game_id: str
    session_id: str
    operation_id: str
    command_id: str
    sequence_no: int
    observed_state_version: int
    processing_status: EventProcessingStatus

    def __post_init__(self) -> None:
        for name in (
            "event_id",
            "game_id",
            "session_id",
            "operation_id",
            "command_id",
        ):
            _require_text(name, getattr(self, name))
        _require_positive_int("sequence_no", self.sequence_no)
        _require_non_negative_int(
            "observed_state_version", self.observed_state_version
        )
        _require_enum(
            "processing_status", self.processing_status, EventProcessingStatus
        )


@dataclass(frozen=True, slots=True)
class ControlResultEventRecoveryEvidence:
    event_id: str
    event_type: GameEventType
    sequence_no: int
    causation_event_id: str
    result_state_version: int
    processing_status: EventProcessingStatus
    game_id: str
    session_id: str
    operation_id: str
    command_id: str

    def __post_init__(self) -> None:
        for name in (
            "event_id",
            "causation_event_id",
            "game_id",
            "session_id",
            "operation_id",
            "command_id",
        ):
            _require_text(name, getattr(self, name))
        _require_enum("event_type", self.event_type, GameEventType)
        _require_positive_int("sequence_no", self.sequence_no)
        _require_non_negative_int(
            "result_state_version", self.result_state_version
        )
        _require_enum(
            "processing_status", self.processing_status, EventProcessingStatus
        )


@dataclass(frozen=True, slots=True)
class ControlNotificationRecoveryEvidence:
    event_id: str
    sequence_no: int
    processing_status: EventProcessingStatus
    last_checkpoint_reference: str

    def __post_init__(self) -> None:
        for name in ("event_id", "last_checkpoint_reference"):
            _require_text(name, getattr(self, name))
        _require_positive_int("sequence_no", self.sequence_no)
        _require_enum(
            "processing_status", self.processing_status, EventProcessingStatus
        )


@dataclass(frozen=True, slots=True)
class ControlCommitEvidence:
    commit_evidence_reference: str
    transaction_kind: ControlCommitTransactionKind
    game_id: str
    session_id: str
    operation_id: str
    command_id: str
    claim_id: str
    input_event_id: str
    expected_state_version: int
    committed_state_version: int
    expected_cursor: int
    committed_cursor: int
    operation_terminal_status: ControlOperationStatus
    result_event_references: tuple[str, ...]
    snapshot_reference: str
    ownership_generation: int | None
    committed_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "commit_evidence_reference",
            "game_id",
            "session_id",
            "operation_id",
            "command_id",
            "claim_id",
            "input_event_id",
            "snapshot_reference",
        ):
            _require_text(name, getattr(self, name))
        _require_enum(
            "transaction_kind", self.transaction_kind, ControlCommitTransactionKind
        )
        for name in (
            "expected_state_version",
            "committed_state_version",
            "expected_cursor",
            "committed_cursor",
        ):
            _require_non_negative_int(name, getattr(self, name))
        _require_enum(
            "operation_terminal_status",
            self.operation_terminal_status,
            ControlOperationStatus,
        )
        references = _freeze_text_tuple(
            "result_event_references", self.result_event_references, required=True
        )
        object.__setattr__(self, "result_event_references", references)
        _require_optional_positive_int(
            "ownership_generation", self.ownership_generation
        )
        _require_aware_time("committed_at", self.committed_at)
        if self.committed_cursor < self.expected_cursor:
            raise ControlRecoveryEvidenceError(
                "committed cursor cannot precede expected cursor"
            )
        if self.transaction_kind is ControlCommitTransactionKind.APPLY:
            if self.operation_terminal_status is not ControlOperationStatus.SUCCESS:
                raise ControlRecoveryEvidenceError(
                    "APPLY commit requires SUCCESS terminal status"
                )
            if self.committed_state_version != self.expected_state_version + 1:
                raise ControlRecoveryEvidenceError(
                    "APPLY committed version must advance expected version once"
                )
        else:
            if self.operation_terminal_status is not ControlOperationStatus.FAILED:
                raise ControlRecoveryEvidenceError(
                    "REJECTION commit requires FAILED terminal status"
                )
            if self.committed_state_version != self.expected_state_version:
                raise ControlRecoveryEvidenceError(
                    "REJECTION committed version must preserve expected version"
                )


def derive_recovery_resolution_id(
    *,
    operation_id: str,
    evidence_snapshot_reference: str,
    disposition: RecoveryResolutionDisposition,
    commit_evidence_reference: str | None,
    result_event_references: tuple[str, ...],
) -> str:
    """Derive one stable append-only resolution identity from persisted facts."""

    _require_text("operation_id", operation_id)
    _require_text("evidence_snapshot_reference", evidence_snapshot_reference)
    _require_enum("disposition", disposition, RecoveryResolutionDisposition)
    _require_optional_text(
        "commit_evidence_reference", commit_evidence_reference
    )
    references = _freeze_text_tuple(
        "result_event_references", result_event_references, required=False
    )
    canonical = (
        "CONTROL_RECOVERY_RESOLUTION_V1",
        operation_id,
        evidence_snapshot_reference,
        disposition.value,
        commit_evidence_reference,
        references,
    )
    encoded = json.dumps(
        canonical, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return f"control-recovery-resolution-v1:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class RecoveryResolutionRecord:
    resolution_id: str
    operation_id: str
    game_id: str
    session_id: str
    raw_operation_status: ControlOperationStatus
    resolution_disposition: RecoveryResolutionDisposition
    evidence_snapshot_reference: str
    commit_evidence_reference: str | None
    result_event_references: tuple[str, ...]
    resolved_at: datetime
    resolver_authority_reference: str

    def __post_init__(self) -> None:
        for name in (
            "resolution_id",
            "operation_id",
            "game_id",
            "session_id",
            "evidence_snapshot_reference",
            "resolver_authority_reference",
        ):
            _require_text(name, getattr(self, name))
        if self.raw_operation_status is not ControlOperationStatus.UNKNOWN:
            raise ControlRecoveryEvidenceError(
                "Recovery Resolution can only interpret an UNKNOWN Operation"
            )
        _require_enum(
            "resolution_disposition",
            self.resolution_disposition,
            RecoveryResolutionDisposition,
        )
        _require_optional_text(
            "commit_evidence_reference", self.commit_evidence_reference
        )
        references = _freeze_text_tuple(
            "result_event_references", self.result_event_references, required=False
        )
        object.__setattr__(self, "result_event_references", references)
        _require_aware_time("resolved_at", self.resolved_at)
        committed = self.resolution_disposition in {
            RecoveryResolutionDisposition.COMMITTED_SUCCESS,
            RecoveryResolutionDisposition.COMMITTED_REJECTION,
        }
        if committed and (
            self.commit_evidence_reference is None or not references
        ):
            raise ControlRecoveryEvidenceError(
                "committed Resolution requires commit and Result Event evidence"
            )
        if (
            self.resolution_disposition
            is RecoveryResolutionDisposition.NO_COMMIT_CONFIRMED
            and (self.commit_evidence_reference is not None or references)
        ):
            raise ControlRecoveryEvidenceError(
                "NO_COMMIT_CONFIRMED cannot claim commit or Result Event evidence"
            )
        expected_id = derive_recovery_resolution_id(
            operation_id=self.operation_id,
            evidence_snapshot_reference=self.evidence_snapshot_reference,
            disposition=self.resolution_disposition,
            commit_evidence_reference=self.commit_evidence_reference,
            result_event_references=references,
        )
        if self.resolution_id != expected_id:
            raise ControlRecoveryEvidenceError(
                "resolution_id does not match immutable recovery evidence"
            )


@dataclass(frozen=True, slots=True)
class ControlRecoveryEvidenceSnapshot:
    contract_version: int
    evidence_snapshot_reference: str
    read_epoch: str
    captured_at: datetime
    recovery_barrier_reference: str
    operation_evidence: ControlOperationRecoveryEvidence
    snapshot_evidence: ControlSnapshotRecoveryEvidence
    input_event_evidence: ControlInputEventRecoveryEvidence
    result_event_evidence: tuple[ControlResultEventRecoveryEvidence, ...]
    commit_evidence: ControlCommitEvidence | None
    notification_evidence: tuple[ControlNotificationRecoveryEvidence, ...]
    existing_resolution: RecoveryResolutionRecord | None

    def __post_init__(self) -> None:
        if self.contract_version != 1 or isinstance(self.contract_version, bool):
            raise ControlRecoveryEvidenceError(
                "unsupported Control Recovery evidence contract version"
            )
        for name in (
            "evidence_snapshot_reference",
            "read_epoch",
            "recovery_barrier_reference",
        ):
            _require_text(name, getattr(self, name))
        _require_aware_time("captured_at", self.captured_at)
        for name, value, expected_type in (
            (
                "operation_evidence",
                self.operation_evidence,
                ControlOperationRecoveryEvidence,
            ),
            (
                "snapshot_evidence",
                self.snapshot_evidence,
                ControlSnapshotRecoveryEvidence,
            ),
            (
                "input_event_evidence",
                self.input_event_evidence,
                ControlInputEventRecoveryEvidence,
            ),
        ):
            _require_type(name, value, expected_type)
        results = _freeze_typed_tuple(
            "result_event_evidence",
            self.result_event_evidence,
            ControlResultEventRecoveryEvidence,
        )
        notifications = _freeze_typed_tuple(
            "notification_evidence",
            self.notification_evidence,
            ControlNotificationRecoveryEvidence,
        )
        object.__setattr__(self, "result_event_evidence", results)
        object.__setattr__(self, "notification_evidence", notifications)
        if self.commit_evidence is not None:
            _require_type(
                "commit_evidence", self.commit_evidence, ControlCommitEvidence
            )
        if self.existing_resolution is not None:
            _require_type(
                "existing_resolution",
                self.existing_resolution,
                RecoveryResolutionRecord,
            )
        validate_control_recovery_evidence(self)


def validate_control_recovery_evidence(
    evidence: ControlRecoveryEvidenceSnapshot,
) -> ControlRecoveryEvidenceSnapshot:
    """Purely validate one already-collected, transaction-consistent snapshot."""

    if not isinstance(evidence, ControlRecoveryEvidenceSnapshot):
        raise TypeError("evidence must be a ControlRecoveryEvidenceSnapshot")
    operation = evidence.operation_evidence
    snapshot = evidence.snapshot_evidence
    input_event = evidence.input_event_evidence
    expected_scope = (operation.game_id, operation.session_id)
    if (snapshot.game_id, snapshot.session_id) != expected_scope or (
        input_event.game_id,
        input_event.session_id,
    ) != expected_scope:
        raise ControlRecoveryEvidenceError(
            "recovery evidence scope does not match Operation scope"
        )
    if (
        input_event.operation_id != operation.operation_id
        or input_event.command_id != operation.command_id
        or input_event.event_id != operation.input_event_id
        or input_event.sequence_no != operation.input_sequence_no
    ):
        raise ControlRecoveryEvidenceError(
            "input Event identity does not match Operation evidence"
        )

    results = evidence.result_event_evidence
    sequences = tuple(result.sequence_no for result in results)
    if len(set(sequences)) != len(sequences) or any(
        current <= previous
        for previous, current in zip(sequences, sequences[1:])
    ):
        raise ControlRecoveryEvidenceError(
            "Result Event sequence must be unique and strictly ascending"
        )
    for result in results:
        if (result.game_id, result.session_id) != expected_scope or (
            result.operation_id != operation.operation_id
            or result.command_id != operation.command_id
        ):
            raise ControlRecoveryEvidenceError(
                "Result Event scope does not match Operation scope"
            )
        if result.causation_event_id != operation.input_event_id:
            raise ControlRecoveryEvidenceError(
                "Result Event causation does not match input Event"
            )
        if result.sequence_no <= operation.input_sequence_no:
            raise ControlRecoveryEvidenceError(
                "Result Event sequence must follow input Event sequence"
            )

    notifications = evidence.notification_evidence
    result_checkpoints = {
        (result.event_id, result.sequence_no): result.processing_status
        for result in results
    }
    notification_sequences = tuple(
        notification.sequence_no for notification in notifications
    )
    if len(set(notification_sequences)) != len(notification_sequences) or any(
        current <= previous
        for previous, current in zip(
            notification_sequences, notification_sequences[1:]
        )
    ):
        raise ControlRecoveryEvidenceError(
            "notification checkpoint sequences must be unique and ascending"
        )
    for notification in notifications:
        result_status = result_checkpoints.get(
            (notification.event_id, notification.sequence_no)
        )
        if result_status is None or result_status is not notification.processing_status:
            raise ControlRecoveryEvidenceError(
                "notification checkpoint does not bind to a Result Event"
            )

    commit = evidence.commit_evidence
    if commit is None:
        if operation.operation_status in {
            ControlOperationStatus.SUCCESS,
            ControlOperationStatus.FAILED,
        }:
            raise ControlRecoveryEvidenceError(
                "terminal Operation requires durable commit evidence"
            )
    else:
        if (commit.game_id, commit.session_id) != expected_scope or (
            commit.operation_id != operation.operation_id
            or commit.command_id != operation.command_id
            or commit.input_event_id != operation.input_event_id
            or commit.claim_id != operation.claim_id
        ):
            raise ControlRecoveryEvidenceError(
                "commit evidence scope or identity does not match Operation"
            )
        if commit.committed_cursor < operation.input_sequence_no:
            raise ControlRecoveryEvidenceError(
                "committed cursor must include the input Event sequence"
            )
        if input_event.observed_state_version != commit.expected_state_version:
            raise ControlRecoveryEvidenceError(
                "input Event observed version does not match commit expected version"
            )
        if commit.expected_cursor + 1 != operation.input_sequence_no:
            raise ControlRecoveryEvidenceError(
                "commit expected cursor must immediately precede input Event sequence"
            )
        if commit.snapshot_reference != snapshot.snapshot_reference:
            raise ControlRecoveryEvidenceError(
                "commit snapshot reference does not match Snapshot evidence"
            )
        expected_version = (
            commit.committed_state_version
            if commit.transaction_kind is ControlCommitTransactionKind.APPLY
            else commit.expected_state_version
        )
        if snapshot.state_version != expected_version:
            raise ControlRecoveryEvidenceError(
                "Snapshot version does not match commit evidence version"
            )
        if snapshot.cursor != commit.committed_cursor:
            raise ControlRecoveryEvidenceError(
                "Snapshot cursor does not match committed cursor"
            )
        result_ids = tuple(result.event_id for result in results)
        if result_ids != commit.result_event_references:
            raise ControlRecoveryEvidenceError(
                "Result Event references are incomplete for commit evidence"
            )
        if any(result.result_state_version != expected_version for result in results):
            raise ControlRecoveryEvidenceError(
                "Result Event version does not match commit evidence version"
            )
        expected_input_status = (
            EventProcessingStatus.APPLIED
            if commit.transaction_kind is ControlCommitTransactionKind.APPLY
            else EventProcessingStatus.REJECTED
        )
        if input_event.processing_status is not expected_input_status:
            raise ControlRecoveryEvidenceError(
                "input Event processing status does not match commit kind"
            )
        if operation.operation_status in {
            ControlOperationStatus.SUCCESS,
            ControlOperationStatus.FAILED,
        } and operation.operation_status is not commit.operation_terminal_status:
            raise ControlRecoveryEvidenceError(
                "Operation terminal status does not match commit evidence"
            )

    resolution = evidence.existing_resolution
    if resolution is not None:
        if (
            resolution.operation_id != operation.operation_id
            or (resolution.game_id, resolution.session_id) != expected_scope
            or operation.operation_status is not ControlOperationStatus.UNKNOWN
        ):
            raise ControlRecoveryEvidenceError(
                "existing Resolution does not bind to UNKNOWN recovery evidence"
            )
        if resolution.resolution_disposition in {
            RecoveryResolutionDisposition.COMMITTED_SUCCESS,
            RecoveryResolutionDisposition.COMMITTED_REJECTION,
        }:
            if commit is None:
                raise ControlRecoveryEvidenceError(
                    "committed Resolution requires durable commit evidence"
                )
            expected_disposition = (
                RecoveryResolutionDisposition.COMMITTED_SUCCESS
                if commit.transaction_kind is ControlCommitTransactionKind.APPLY
                else RecoveryResolutionDisposition.COMMITTED_REJECTION
            )
            if (
                resolution.resolution_disposition is not expected_disposition
                or resolution.commit_evidence_reference
                != commit.commit_evidence_reference
                or resolution.result_event_references
                != commit.result_event_references
            ):
                raise ControlRecoveryEvidenceError(
                    "Resolution disposition does not match commit evidence"
                )
        elif (
            resolution.resolution_disposition
            is RecoveryResolutionDisposition.NO_COMMIT_CONFIRMED
            and commit is not None
        ):
            raise ControlRecoveryEvidenceError(
                "NO_COMMIT_CONFIRMED Resolution contradicts commit evidence"
            )
    return evidence


@dataclass(frozen=True, slots=True)
class RecoveryCommitted:
    game_id: str
    session_id: str
    operation_id: str
    evidence_snapshot_reference: str
    commit_evidence_reference: str
    resolved_terminal_status: ControlOperationStatus
    snapshot_reference: str
    committed_state_version: int
    committed_cursor: int
    result_event_references: tuple[str, ...]
    notification_replay_references: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_outcome_scope(self)
        for name in ("commit_evidence_reference", "snapshot_reference"):
            _require_text(name, getattr(self, name))
        if self.resolved_terminal_status not in {
            ControlOperationStatus.SUCCESS,
            ControlOperationStatus.FAILED,
        }:
            raise ControlRecoveryEvidenceError(
                "RecoveryCommitted requires a committed terminal status"
            )
        _require_non_negative_int(
            "committed_state_version", self.committed_state_version
        )
        _require_non_negative_int("committed_cursor", self.committed_cursor)
        results = _freeze_text_tuple(
            "result_event_references", self.result_event_references, required=True
        )
        replay = _freeze_text_tuple(
            "notification_replay_references",
            self.notification_replay_references,
            required=False,
        )
        if any(reference not in results for reference in replay):
            raise ControlRecoveryEvidenceError(
                "notification replay references must be committed Result Events"
            )
        object.__setattr__(self, "result_event_references", results)
        object.__setattr__(self, "notification_replay_references", replay)


@dataclass(frozen=True, slots=True)
class RecoveryReadmissionAllowed:
    game_id: str
    session_id: str
    operation_id: str
    input_event_id: str
    evidence_snapshot_reference: str
    stable_claim_id: str

    def __post_init__(self) -> None:
        _validate_outcome_scope(self)
        for name in ("input_event_id", "stable_claim_id"):
            _require_text(name, getattr(self, name))


@dataclass(frozen=True, slots=True)
class RecoveryUnknown:
    game_id: str
    session_id: str
    operation_id: str
    evidence_snapshot_reference: str
    reason: RecoveryUnknownReason

    def __post_init__(self) -> None:
        _validate_outcome_scope(self)
        _require_enum("reason", self.reason, RecoveryUnknownReason)


@dataclass(frozen=True, slots=True)
class RecoveryFault:
    game_id: str
    session_id: str
    operation_id: str
    evidence_snapshot_reference: str
    reason: RecoveryFaultReason

    def __post_init__(self) -> None:
        _validate_outcome_scope(self)
        _require_enum("reason", self.reason, RecoveryFaultReason)


RecoveryOutcome: TypeAlias = (
    RecoveryCommitted
    | RecoveryReadmissionAllowed
    | RecoveryUnknown
    | RecoveryFault
)


def _validate_outcome_scope(value: object) -> None:
    for name in (
        "game_id",
        "session_id",
        "operation_id",
        "evidence_snapshot_reference",
    ):
        _require_text(name, getattr(value, name))


def _require_type(name: str, value: object, expected_type: type[object]) -> None:
    if not isinstance(value, expected_type):
        raise TypeError(f"{name} must be a {expected_type.__name__}")


def _require_enum(name: str, value: object, enum_type: type[Enum]) -> None:
    if not isinstance(value, enum_type):
        raise TypeError(f"{name} must be a {enum_type.__name__}")


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ControlRecoveryEvidenceError(f"{name} must be non-empty text")


def _require_optional_text(name: str, value: object) -> None:
    if value is not None:
        _require_text(name, value)


def _require_non_negative_int(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ControlRecoveryEvidenceError(
            f"{name} must be a non-negative integer"
        )


def _require_positive_int(name: str, value: object) -> None:
    _require_non_negative_int(name, value)
    if value == 0:
        raise ControlRecoveryEvidenceError(f"{name} must be a positive integer")


def _require_optional_positive_int(name: str, value: object) -> None:
    if value is not None:
        _require_positive_int(name, value)


def _require_aware_time(name: str, value: object) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ControlRecoveryEvidenceError(f"{name} must be timezone-aware")


def _require_optional_aware_time(name: str, value: object) -> None:
    if value is not None:
        _require_aware_time(name, value)


def _freeze_text_tuple(
    name: str, values: object, *, required: bool
) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise TypeError(f"{name} must be a tuple or list")
    frozen = tuple(values)
    if required and not frozen:
        raise ControlRecoveryEvidenceError(f"{name} must not be empty")
    for value in frozen:
        _require_text(name, value)
    if len(set(frozen)) != len(frozen):
        raise ControlRecoveryEvidenceError(f"{name} must contain unique references")
    return frozen


def _freeze_typed_tuple(
    name: str, values: object, expected_type: type[object]
) -> tuple[object, ...]:
    if not isinstance(values, (tuple, list)):
        raise TypeError(f"{name} must be a tuple or list")
    frozen = tuple(values)
    if any(not isinstance(value, expected_type) for value in frozen):
        raise TypeError(f"{name} contains invalid evidence")
    return frozen
