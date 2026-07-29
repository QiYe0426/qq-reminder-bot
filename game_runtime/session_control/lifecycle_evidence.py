"""Immutable lifecycle and phase evidence for P3-D-6.6 plan building."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from enum import Enum

from game_runtime.session import GamePhase
from game_runtime.session_control.commands import SessionCommandType


class ControlLifecycleEvidenceError(ValueError):
    """Raised when lifecycle evidence is incomplete or inconsistently bound."""


class StartReadinessStatus(str, Enum):
    READY = "READY"
    NOT_READY = "NOT_READY"
    UNKNOWN = "UNKNOWN"


class HunterInstanceReadinessStatus(str, Enum):
    READY = "READY"
    NOT_READY = "NOT_READY"
    UNKNOWN = "UNKNOWN"


class ResumeValidationStatus(str, Enum):
    READY = "READY"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class PhaseVisibilityDecision(str, Enum):
    NO_CHANGE = "NO_CHANGE"
    CHANGE_SET = "CHANGE_SET"


@dataclass(frozen=True, slots=True)
class ControlStartReadinessEvidence:
    """Persisted readiness certification consumed by START_GAME planning."""

    contract_version: int
    game_id: str
    session_id: str
    observed_state_version: int
    readiness_status: StartReadinessStatus
    readiness_evidence_reference: str
    setup_manifest_reference: str
    setup_manifest_version: int
    participant_roster_reference: str
    participant_roster_version: int
    character_assignment_set_reference: str
    character_assignment_version: int
    hunter_instance_reference: str
    hunter_instance_version: int
    hunter_instance_status: HunterInstanceReadinessStatus
    policy_reference: str
    policy_version: int
    template_reference: str
    template_version: int
    knowledge_partition_reference: str
    knowledge_partition_version: int

    def __post_init__(self) -> None:
        _require_contract_version(self.contract_version)
        for name in (
            "game_id",
            "session_id",
            "readiness_evidence_reference",
            "setup_manifest_reference",
            "participant_roster_reference",
            "character_assignment_set_reference",
            "hunter_instance_reference",
            "policy_reference",
            "template_reference",
            "knowledge_partition_reference",
        ):
            _require_text(name, getattr(self, name))
        for name in (
            "observed_state_version",
            "setup_manifest_version",
            "participant_roster_version",
            "character_assignment_version",
            "hunter_instance_version",
            "policy_version",
            "template_version",
            "knowledge_partition_version",
        ):
            _require_non_negative_int(name, getattr(self, name))
        _require_enum("readiness_status", self.readiness_status, StartReadinessStatus)
        _require_enum(
            "hunter_instance_status",
            self.hunter_instance_status,
            HunterInstanceReadinessStatus,
        )
        if (
            self.readiness_status is StartReadinessStatus.READY
            and self.hunter_instance_status is not HunterInstanceReadinessStatus.READY
        ):
            raise ControlLifecycleEvidenceError(
                "READY start evidence requires a READY Hunter instance"
            )


@dataclass(frozen=True, slots=True)
class ControlResumeValidationEvidence:
    """Recovery, pause, and retained-ownership proof for RESUME_GAME."""

    contract_version: int
    game_id: str
    session_id: str
    observed_state_version: int
    validation_status: ResumeValidationStatus
    recovery_validation_reference: str
    validated_snapshot_reference: str
    validated_state_version: int
    validated_cursor: int
    previous_pause_event_reference: str
    previous_pause_state_version: int
    previous_pause_sequence_no: int
    ownership_evidence_reference: str
    ownership_generation: int

    def __post_init__(self) -> None:
        _require_contract_version(self.contract_version)
        for name in (
            "game_id",
            "session_id",
            "recovery_validation_reference",
            "validated_snapshot_reference",
            "previous_pause_event_reference",
            "ownership_evidence_reference",
        ):
            _require_text(name, getattr(self, name))
        for name in (
            "observed_state_version",
            "validated_state_version",
            "validated_cursor",
            "previous_pause_state_version",
            "previous_pause_sequence_no",
        ):
            _require_non_negative_int(name, getattr(self, name))
        _require_positive_int("ownership_generation", self.ownership_generation)
        _require_enum(
            "validation_status", self.validation_status, ResumeValidationStatus
        )
        if self.validated_state_version != self.observed_state_version:
            raise ControlLifecycleEvidenceError(
                "validated_state_version must match observed_state_version"
            )
        if self.previous_pause_state_version != self.observed_state_version:
            raise ControlLifecycleEvidenceError(
                "previous pause evidence must match observed_state_version"
            )
        if self.previous_pause_sequence_no != self.validated_cursor:
            raise ControlLifecycleEvidenceError(
                "previous pause sequence must match validated cursor"
            )


@dataclass(frozen=True, slots=True)
class ControlEndRetentionEvidence:
    """Retention proof for END_GAME, separate from any public result reference."""

    contract_version: int
    game_id: str
    session_id: str
    observed_state_version: int
    retention_policy_reference: str
    retention_policy_version: int
    retention_reference: str
    retention_timestamp: datetime
    public_result_reference: str | None = None

    def __post_init__(self) -> None:
        _require_contract_version(self.contract_version)
        for name in (
            "game_id",
            "session_id",
            "retention_policy_reference",
            "retention_reference",
        ):
            _require_text(name, getattr(self, name))
        _require_non_negative_int(
            "observed_state_version", self.observed_state_version
        )
        _require_non_negative_int(
            "retention_policy_version", self.retention_policy_version
        )
        _require_aware_time("retention_timestamp", self.retention_timestamp)
        if self.public_result_reference is not None:
            _require_text("public_result_reference", self.public_result_reference)
            if self.public_result_reference == self.retention_reference:
                raise ControlLifecycleEvidenceError(
                    "public_result_reference must not equal retention_reference"
                )


@dataclass(frozen=True, slots=True)
class ControlPhaseVisibilityIntent:
    """Opaque visibility intent committed atomically with one phase transition."""

    contract_version: int
    game_id: str
    session_id: str
    expected_state_version: int
    previous_phase: GamePhase
    target_phase: GamePhase
    visibility_policy_reference: str
    visibility_policy_version: int
    decision: PhaseVisibilityDecision
    change_set_reference: str | None
    intent_reference: str

    def __post_init__(self) -> None:
        _require_contract_version(self.contract_version)
        for name in (
            "game_id",
            "session_id",
            "visibility_policy_reference",
            "intent_reference",
        ):
            _require_text(name, getattr(self, name))
        _require_non_negative_int(
            "expected_state_version", self.expected_state_version
        )
        _require_non_negative_int(
            "visibility_policy_version", self.visibility_policy_version
        )
        _require_enum("previous_phase", self.previous_phase, GamePhase)
        _require_enum("target_phase", self.target_phase, GamePhase)
        _require_enum("decision", self.decision, PhaseVisibilityDecision)
        if self.previous_phase is self.target_phase:
            raise ControlLifecycleEvidenceError(
                "phase visibility intent requires a phase change"
            )
        if self.decision is PhaseVisibilityDecision.CHANGE_SET:
            _require_text("change_set_reference", self.change_set_reference)
        elif self.change_set_reference is not None:
            raise ControlLifecycleEvidenceError(
                "NO_CHANGE visibility intent must not carry a change set"
            )


@dataclass(frozen=True, slots=True)
class ControlLifecycleApplyEvidence:
    """Command-scoped lifecycle evidence bundle for a pure build context."""

    start_readiness: ControlStartReadinessEvidence | None = None
    resume_validation: ControlResumeValidationEvidence | None = None
    end_retention: ControlEndRetentionEvidence | None = None
    phase_visibility_intent: ControlPhaseVisibilityIntent | None = None

    def __post_init__(self) -> None:
        expected_types = {
            "start_readiness": ControlStartReadinessEvidence,
            "resume_validation": ControlResumeValidationEvidence,
            "end_retention": ControlEndRetentionEvidence,
            "phase_visibility_intent": ControlPhaseVisibilityIntent,
        }
        for item in fields(self):
            value = getattr(self, item.name)
            if value is not None and not isinstance(value, expected_types[item.name]):
                raise ControlLifecycleEvidenceError(
                    f"{item.name} has invalid lifecycle evidence type"
                )

    def validate_for_command(
        self,
        command_type: SessionCommandType,
        *,
        game_id: str,
        session_id: str,
        observed_state_version: int,
    ) -> None:
        """Fail closed unless the exact command evidence set and binding match."""

        self.validate_bindings(
            game_id=game_id,
            session_id=session_id,
            observed_state_version=observed_state_version,
        )
        if not isinstance(command_type, SessionCommandType):
            raise ControlLifecycleEvidenceError(
                "command_type must be a SessionCommandType"
            )
        if command_type is SessionCommandType.CREATE_SESSION:
            raise ControlLifecycleEvidenceError(
                "CREATE_SESSION must use the bootstrap evidence path"
            )

        required: dict[SessionCommandType, frozenset[str]] = {
            SessionCommandType.START_GAME: frozenset(
                {"start_readiness", "phase_visibility_intent"}
            ),
            SessionCommandType.RESUME_GAME: frozenset({"resume_validation"}),
            SessionCommandType.END_GAME: frozenset({"end_retention"}),
            SessionCommandType.CHANGE_PHASE: frozenset(
                {"phase_visibility_intent"}
            ),
        }
        present = frozenset(
            item.name for item in fields(self) if getattr(self, item.name) is not None
        )
        expected = required.get(command_type, frozenset())
        if present != expected:
            raise ControlLifecycleEvidenceError(
                f"{command_type.value} requires lifecycle evidence {sorted(expected)}"
            )

    def validate_bindings(
        self,
        *,
        game_id: str,
        session_id: str,
        observed_state_version: int,
    ) -> None:
        """Validate only evidence that is present, without requiring completeness."""

        _require_text("game_id", game_id)
        _require_text("session_id", session_id)
        _require_non_negative_int("observed_state_version", observed_state_version)
        present = frozenset(
            item.name for item in fields(self) if getattr(self, item.name) is not None
        )
        for name in present:
            evidence = getattr(self, name)
            evidence_version = getattr(
                evidence,
                "expected_state_version",
                getattr(evidence, "observed_state_version", None),
            )
            if (evidence.game_id, evidence.session_id) != (game_id, session_id):
                raise ControlLifecycleEvidenceError(
                    f"{name} scope does not match Actor turn"
                )
            if evidence_version != observed_state_version:
                raise ControlLifecycleEvidenceError(
                    f"{name} state version does not match Actor turn"
                )


def _require_contract_version(value: object) -> None:
    if value != 1:
        raise ControlLifecycleEvidenceError("unsupported lifecycle contract version")


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ControlLifecycleEvidenceError(f"{name} must be non-empty text")


def _require_non_negative_int(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ControlLifecycleEvidenceError(
            f"{name} must be a non-negative integer"
        )


def _require_positive_int(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ControlLifecycleEvidenceError(f"{name} must be a positive integer")


def _require_aware_time(name: str, value: object) -> None:
    if not isinstance(value, datetime):
        raise ControlLifecycleEvidenceError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ControlLifecycleEvidenceError(f"{name} must be timezone-aware")


def _require_enum(name: str, value: object, enum_type: type[Enum]) -> None:
    if not isinstance(value, enum_type):
        raise ControlLifecycleEvidenceError(
            f"{name} must be a {enum_type.__name__}"
        )
