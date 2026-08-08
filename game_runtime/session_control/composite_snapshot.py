"""Immutable composite Game Runtime snapshot contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from game_runtime.participant import (
    ParticipantMembershipState,
    ParticipantType,
)
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control.apply_contract import CandidateSessionSnapshot


COMPOSITE_SNAPSHOT_SCHEMA_VERSION = 2
DOMAIN_SLICE_SCHEMA_VERSION = 1
GAME_RULE_SLICE_SCHEMA_VERSION = 2


class CompositeSnapshotFailureReason(str, Enum):
    UNSUPPORTED_SCHEMA_VERSION = "UNSUPPORTED_SCHEMA_VERSION"
    INVALID_DOMAIN_VERSION = "INVALID_DOMAIN_VERSION"
    INCOMPLETE_SLICE = "INCOMPLETE_SLICE"
    INVALID_SLICE_VALUE = "INVALID_SLICE_VALUE"
    LEGACY_PROJECTION_MISMATCH = "LEGACY_PROJECTION_MISMATCH"
    NON_CANONICAL_PARTICIPANTS = "NON_CANONICAL_PARTICIPANTS"
    DUPLICATE_PARTICIPANT = "DUPLICATE_PARTICIPANT"
    DM_BINDING_MISMATCH = "DM_BINDING_MISMATCH"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    INVALID_CONTROL_CURSOR = "INVALID_CONTROL_CURSOR"
    COMPLETION_IDENTITY_MISMATCH = "COMPLETION_IDENTITY_MISMATCH"
    GAME_RULE_HIDDEN_BINDING_MISMATCH = "GAME_RULE_HIDDEN_BINDING_MISMATCH"
    GAME_RULE_DISCLOSURE_BINDING_MISMATCH = (
        "GAME_RULE_DISCLOSURE_BINDING_MISMATCH"
    )


class CompositeSnapshotContractError(ValueError):
    """Typed semantic rejection for an invalid composite snapshot value."""

    def __init__(self, reason: CompositeSnapshotFailureReason) -> None:
        if not isinstance(reason, CompositeSnapshotFailureReason):
            raise TypeError("reason must be a CompositeSnapshotFailureReason")
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True)
class LifecycleSnapshotSlice:
    schema_version: int
    domain_version: int
    status: GameSessionStatus

    def __post_init__(self) -> None:
        _validate_schema_version(self.schema_version)
        _validate_domain_version(self.domain_version)
        if not isinstance(self.status, GameSessionStatus):
            raise TypeError("status must be a GameSessionStatus")


@dataclass(frozen=True, slots=True)
class PhaseSnapshotSlice:
    schema_version: int
    domain_version: int
    phase: GamePhase

    def __post_init__(self) -> None:
        _validate_schema_version(self.schema_version)
        _validate_domain_version(self.domain_version)
        if not isinstance(self.phase, GamePhase):
            raise TypeError("phase must be a GamePhase")


@dataclass(frozen=True, slots=True)
class SetupSnapshotSlice:
    schema_version: int
    domain_version: int
    script_id: str | None
    public_name: str | None
    manifest_reference: str | None
    manifest_version: int | None

    def __post_init__(self) -> None:
        _validate_schema_version(self.schema_version)
        _validate_domain_version(self.domain_version)
        values = (
            self.script_id,
            self.public_name,
            self.manifest_reference,
            self.manifest_version,
        )
        present = tuple(value is not None for value in values)
        if any(present) and not all(present):
            _fail(CompositeSnapshotFailureReason.INCOMPLETE_SLICE)
        if not any(present):
            if self.domain_version != 0:
                _fail(CompositeSnapshotFailureReason.INVALID_DOMAIN_VERSION)
            return
        for name in ("script_id", "public_name", "manifest_reference"):
            _require_text(name, getattr(self, name))
        _require_non_negative_int("manifest_version", self.manifest_version)
        if self.domain_version == 0:
            _fail(CompositeSnapshotFailureReason.INVALID_DOMAIN_VERSION)


@dataclass(frozen=True, slots=True)
class ParticipantSnapshotRecord:
    participant_id: str
    participant_type: ParticipantType
    membership_state: ParticipantMembershipState
    character_id: str | None
    binding_version: int

    def __post_init__(self) -> None:
        _require_text("participant_id", self.participant_id)
        if not isinstance(self.participant_type, ParticipantType):
            raise TypeError("participant_type must be a ParticipantType")
        if not isinstance(self.membership_state, ParticipantMembershipState):
            raise TypeError(
                "membership_state must be a ParticipantMembershipState"
            )
        if self.character_id is not None:
            _require_text("character_id", self.character_id)
        _require_non_negative_int("binding_version", self.binding_version)


@dataclass(frozen=True, slots=True)
class ParticipantSnapshotSlice:
    schema_version: int
    domain_version: int
    participants: tuple[ParticipantSnapshotRecord, ...]

    def __post_init__(self) -> None:
        _validate_schema_version(self.schema_version)
        _validate_domain_version(self.domain_version)
        if not isinstance(self.participants, tuple):
            raise TypeError("participants must be a tuple")
        if not all(
            isinstance(participant, ParticipantSnapshotRecord)
            for participant in self.participants
        ):
            raise TypeError(
                "participants must contain ParticipantSnapshotRecord values"
            )
        identities = tuple(
            participant.participant_id for participant in self.participants
        )
        if len(set(identities)) != len(identities):
            _fail(CompositeSnapshotFailureReason.DUPLICATE_PARTICIPANT)
        if identities != tuple(sorted(identities)):
            _fail(CompositeSnapshotFailureReason.NON_CANONICAL_PARTICIPANTS)
        if self.participants and self.domain_version == 0:
            _fail(CompositeSnapshotFailureReason.INVALID_DOMAIN_VERSION)
        if not self.participants and self.domain_version != 0:
            _fail(CompositeSnapshotFailureReason.INVALID_DOMAIN_VERSION)


@dataclass(frozen=True, slots=True)
class GameRuleSnapshotSlice:
    schema_version: int
    domain_version: int
    committed_rule_set_reference: str | None
    committed_disclosure_state_reference: str | None

    def __post_init__(self) -> None:
        _validate_schema_version(
            self.schema_version,
            expected=GAME_RULE_SLICE_SCHEMA_VERSION,
        )
        _validate_domain_version(self.domain_version)
        rule_present = self.committed_rule_set_reference is not None
        disclosure_present = self.committed_disclosure_state_reference is not None
        if rule_present != disclosure_present:
            _fail(
                CompositeSnapshotFailureReason.GAME_RULE_DISCLOSURE_BINDING_MISMATCH
            )
        if not rule_present:
            if self.domain_version != 0:
                _fail(CompositeSnapshotFailureReason.INVALID_DOMAIN_VERSION)
            return
        _require_text(
            "committed_rule_set_reference", self.committed_rule_set_reference
        )
        _require_text(
            "committed_disclosure_state_reference",
            self.committed_disclosure_state_reference,
        )
        if self.domain_version == 0:
            _fail(CompositeSnapshotFailureReason.INVALID_DOMAIN_VERSION)


@dataclass(frozen=True, slots=True)
class HiddenGameStateSlice:
    schema_version: int
    domain_version: int
    committed_state_reference: str | None

    def __post_init__(self) -> None:
        _validate_optional_reference_slice(
            self.schema_version,
            self.domain_version,
            self.committed_state_reference,
            field_name="committed_state_reference",
        )


@dataclass(frozen=True, slots=True)
class CandidateGameSnapshot(CandidateSessionSnapshot):
    snapshot_schema_version: int
    lifecycle: LifecycleSnapshotSlice
    phase: PhaseSnapshotSlice
    setup: SetupSnapshotSlice
    participants: ParticipantSnapshotSlice
    game_rules: GameRuleSnapshotSlice
    hidden_state: HiddenGameStateSlice

    def __post_init__(self) -> None:
        CandidateSessionSnapshot.__post_init__(self)
        _validate_schema_version(
            self.snapshot_schema_version,
            expected=COMPOSITE_SNAPSHOT_SCHEMA_VERSION,
        )
        expected_types = (
            ("lifecycle", self.lifecycle, LifecycleSnapshotSlice),
            ("phase", self.phase, PhaseSnapshotSlice),
            ("setup", self.setup, SetupSnapshotSlice),
            ("participants", self.participants, ParticipantSnapshotSlice),
            ("game_rules", self.game_rules, GameRuleSnapshotSlice),
            ("hidden_state", self.hidden_state, HiddenGameStateSlice),
        )
        for name, value, expected_type in expected_types:
            if not isinstance(value, expected_type):
                raise TypeError(f"{name} must be a {expected_type.__name__}")
        if (
            self.game_rules.committed_rule_set_reference is None
        ) != (self.hidden_state.committed_state_reference is None):
            _fail(
                CompositeSnapshotFailureReason.GAME_RULE_HIDDEN_BINDING_MISMATCH
            )
        if (
            self.status is not self.lifecycle.status
            or self.current_phase is not self.phase.phase
        ):
            _fail(CompositeSnapshotFailureReason.LEGACY_PROJECTION_MISMATCH)
        dm_records = tuple(
            participant
            for participant in self.participants.participants
            if participant.participant_type is ParticipantType.DM
        )
        if (
            len(dm_records) != 1
            or dm_records[0].participant_id != self.dm_participant_id
        ):
            _fail(CompositeSnapshotFailureReason.DM_BINDING_MISMATCH)


@dataclass(frozen=True, slots=True)
class GameSnapshotIdentity:
    game_id: str
    session_id: str
    group_id: str
    dm_participant_id: str
    state_version: int
    snapshot_materialization_cursor: int
    snapshot_schema_version: int
    domain_versions: tuple[int, int, int, int, int, int]

    def __post_init__(self) -> None:
        for name in ("game_id", "session_id", "group_id", "dm_participant_id"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(f"{name} must be text")
            if not value.strip():
                _fail(CompositeSnapshotFailureReason.SCOPE_MISMATCH)
        _require_non_negative_int("state_version", self.state_version)
        _require_non_negative_int(
            "snapshot_materialization_cursor",
            self.snapshot_materialization_cursor,
        )
        _validate_schema_version(
            self.snapshot_schema_version,
            expected=COMPOSITE_SNAPSHOT_SCHEMA_VERSION,
        )
        if not isinstance(self.domain_versions, tuple):
            raise TypeError("domain_versions must be a tuple")
        if len(self.domain_versions) != 6:
            _fail(CompositeSnapshotFailureReason.INCOMPLETE_SLICE)
        for version in self.domain_versions:
            _validate_domain_version(version)

    @classmethod
    def from_snapshot(
        cls,
        snapshot: CandidateGameSnapshot,
    ) -> GameSnapshotIdentity:
        if not isinstance(snapshot, CandidateGameSnapshot):
            raise TypeError("snapshot must be a CandidateGameSnapshot")
        return cls(
            game_id=snapshot.game_id,
            session_id=snapshot.session_id,
            group_id=snapshot.group_id,
            dm_participant_id=snapshot.dm_participant_id,
            state_version=snapshot.state_version,
            snapshot_materialization_cursor=snapshot.last_applied_sequence_no,
            snapshot_schema_version=snapshot.snapshot_schema_version,
            domain_versions=(
                snapshot.lifecycle.domain_version,
                snapshot.phase.domain_version,
                snapshot.setup.domain_version,
                snapshot.participants.domain_version,
                snapshot.game_rules.domain_version,
                snapshot.hidden_state.domain_version,
            ),
        )


def _validate_schema_version(
    value: object,
    *,
    expected: int = DOMAIN_SLICE_SCHEMA_VERSION,
) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value != expected:
        _fail(CompositeSnapshotFailureReason.UNSUPPORTED_SCHEMA_VERSION)


def _validate_domain_version(value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _fail(CompositeSnapshotFailureReason.INVALID_DOMAIN_VERSION)


def _validate_optional_reference_slice(
    schema_version: object,
    domain_version: object,
    reference: object,
    *,
    field_name: str,
) -> None:
    _validate_schema_version(schema_version)
    _validate_domain_version(domain_version)
    if reference is None:
        if domain_version != 0:
            _fail(CompositeSnapshotFailureReason.INVALID_DOMAIN_VERSION)
        return
    _require_text(field_name, reference)
    if domain_version == 0:
        _fail(CompositeSnapshotFailureReason.INVALID_DOMAIN_VERSION)


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text")
    if not value.strip():
        _fail(CompositeSnapshotFailureReason.INVALID_SLICE_VALUE)


def _require_non_negative_int(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        _fail(CompositeSnapshotFailureReason.INVALID_SLICE_VALUE)


def _fail(reason: CompositeSnapshotFailureReason) -> None:
    raise CompositeSnapshotContractError(reason)
