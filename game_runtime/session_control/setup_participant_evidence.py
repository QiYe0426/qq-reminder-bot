"""Immutable Setup / Participant Apply evidence for P3-D-6.7."""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum
from typing import TypeAlias

from game_runtime.session_control.commands import (
    AssignCharacterPayload,
    ReplacePlayerPayload,
    SessionCommandPayload,
    SessionCommandType,
    SetScriptPayload,
)


class ControlSetupParticipantEvidenceError(ValueError):
    """Raised when Setup / Participant evidence is incomplete or mismatched."""


class SetupParticipantEvidenceStatus(str, Enum):
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class CharacterAvailabilityStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    ASSIGNED_TO_TARGET = "ASSIGNED_TO_TARGET"
    ASSIGNED_TO_OTHER = "ASSIGNED_TO_OTHER"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ControlCharacterAvailabilityEvidence:
    """Persisted roster observation consumed without querying the roster."""

    contract_version: int
    game_id: str
    session_id: str
    observed_state_version: int
    character_id: str
    target_participant_reference: str
    assignment_set_reference: str
    assignment_set_version: int
    availability_status: CharacterAvailabilityStatus
    occupying_participant_reference: str | None

    def __post_init__(self) -> None:
        if self.contract_version != 1 or isinstance(self.contract_version, bool):
            raise ControlSetupParticipantEvidenceError(
                "unsupported character availability contract version"
            )
        for name in (
            "game_id",
            "session_id",
            "character_id",
            "target_participant_reference",
            "assignment_set_reference",
        ):
            _require_text(name, getattr(self, name))
        _require_non_negative_int(
            "observed_state_version", self.observed_state_version
        )
        _require_non_negative_int(
            "assignment_set_version", self.assignment_set_version
        )
        if not isinstance(self.availability_status, CharacterAvailabilityStatus):
            raise ControlSetupParticipantEvidenceError(
                "availability_status must be a CharacterAvailabilityStatus"
            )
        occupant = self.occupying_participant_reference
        if self.availability_status in {
            CharacterAvailabilityStatus.AVAILABLE,
            CharacterAvailabilityStatus.UNKNOWN,
        }:
            if occupant is not None:
                raise ControlSetupParticipantEvidenceError(
                    "AVAILABLE or UNKNOWN availability cannot identify an occupant"
                )
        elif self.availability_status is CharacterAvailabilityStatus.ASSIGNED_TO_TARGET:
            if occupant != self.target_participant_reference:
                raise ControlSetupParticipantEvidenceError(
                    "ASSIGNED_TO_TARGET occupant must equal target participant"
                )
        else:
            _require_text("occupying_participant_reference", occupant)
            if occupant == self.target_participant_reference:
                raise ControlSetupParticipantEvidenceError(
                    "ASSIGNED_TO_OTHER occupant must differ from target participant"
                )


@dataclass(frozen=True, slots=True)
class ControlScriptApplyEvidence:
    """Public-metadata-only evidence for one SET_SCRIPT projection change."""

    contract_version: int
    game_id: str
    session_id: str
    observed_state_version: int
    validation_status: SetupParticipantEvidenceStatus
    script_id: str
    manifest_reference: str
    manifest_version: int
    expected_setup_version: int
    resulting_setup_version: int
    ownership_evidence_reference: str
    ownership_generation: int | None
    visibility_boundary_reference: str

    def __post_init__(self) -> None:
        _validate_common(self)
        for name in (
            "script_id",
            "manifest_reference",
            "ownership_evidence_reference",
            "visibility_boundary_reference",
        ):
            _require_text(name, getattr(self, name))
        _require_non_negative_int("manifest_version", self.manifest_version)
        _require_single_step(
            "setup version",
            self.expected_setup_version,
            self.resulting_setup_version,
        )
        _require_optional_positive_int(
            "ownership_generation", self.ownership_generation
        )


@dataclass(frozen=True, slots=True)
class ControlCharacterAssignmentEvidence:
    """Session-local character binding evidence without character content."""

    contract_version: int
    game_id: str
    session_id: str
    observed_state_version: int
    validation_status: SetupParticipantEvidenceStatus
    participant_id: str
    participant_reference: str
    character_id: str
    character_binding_reference: str
    expected_binding_version: int
    resulting_binding_version: int
    assignment_version: int
    ownership_evidence_reference: str
    ownership_generation: int | None
    authorization_reference: str
    availability_evidence: ControlCharacterAvailabilityEvidence

    def __post_init__(self) -> None:
        _validate_common(self)
        for name in (
            "participant_id",
            "participant_reference",
            "character_id",
            "character_binding_reference",
            "ownership_evidence_reference",
            "authorization_reference",
        ):
            _require_text(name, getattr(self, name))
        if self.participant_reference != self.participant_id:
            raise ControlSetupParticipantEvidenceError(
                "participant_reference must equal participant_id"
            )
        _require_single_step(
            "binding version",
            self.expected_binding_version,
            self.resulting_binding_version,
        )
        _require_positive_int("assignment_version", self.assignment_version)
        if self.assignment_version != self.resulting_binding_version:
            raise ControlSetupParticipantEvidenceError(
                "assignment_version must equal resulting_binding_version"
            )
        _require_optional_positive_int(
            "ownership_generation", self.ownership_generation
        )
        if not isinstance(
            self.availability_evidence, ControlCharacterAvailabilityEvidence
        ):
            raise ControlSetupParticipantEvidenceError(
                "availability_evidence must be ControlCharacterAvailabilityEvidence"
            )
        availability = self.availability_evidence
        if (
            availability.game_id != self.game_id
            or availability.session_id != self.session_id
            or availability.observed_state_version != self.observed_state_version
            or availability.character_id != self.character_id
            or availability.target_participant_reference != self.participant_reference
        ):
            raise ControlSetupParticipantEvidenceError(
                "character availability evidence does not match assignment evidence"
            )


@dataclass(frozen=True, slots=True)
class ControlPlayerReplacementEvidence:
    """Bilateral binding and governance evidence for REPLACE_PLAYER."""

    contract_version: int
    game_id: str
    session_id: str
    observed_state_version: int
    validation_status: SetupParticipantEvidenceStatus
    old_participant_id: str
    old_participant_reference: str
    new_participant_id: str
    new_participant_reference: str
    old_expected_binding_version: int
    old_resulting_binding_version: int
    new_expected_binding_version: int
    new_resulting_binding_version: int
    authorization_reference: str
    confirmation_reference: str
    replacement_policy_reference: str
    character_binding_reference: str | None
    ownership_evidence_reference: str
    ownership_generation: int | None

    def __post_init__(self) -> None:
        _validate_common(self)
        for name in (
            "old_participant_id",
            "old_participant_reference",
            "new_participant_id",
            "new_participant_reference",
            "authorization_reference",
            "confirmation_reference",
            "replacement_policy_reference",
            "ownership_evidence_reference",
        ):
            _require_text(name, getattr(self, name))
        if self.old_participant_id == self.new_participant_id:
            raise ControlSetupParticipantEvidenceError(
                "replacement participants must be different"
            )
        if self.old_participant_reference != self.old_participant_id:
            raise ControlSetupParticipantEvidenceError(
                "old_participant_reference must equal old_participant_id"
            )
        if self.new_participant_reference != self.new_participant_id:
            raise ControlSetupParticipantEvidenceError(
                "new_participant_reference must equal new_participant_id"
            )
        _require_single_step(
            "old binding version",
            self.old_expected_binding_version,
            self.old_resulting_binding_version,
        )
        _require_single_step(
            "new binding version",
            self.new_expected_binding_version,
            self.new_resulting_binding_version,
        )
        if self.character_binding_reference is not None:
            _require_text(
                "character_binding_reference", self.character_binding_reference
            )
        _require_optional_positive_int(
            "ownership_generation", self.ownership_generation
        )


SetupParticipantEvidence: TypeAlias = (
    ControlScriptApplyEvidence
    | ControlCharacterAssignmentEvidence
    | ControlPlayerReplacementEvidence
)


@dataclass(frozen=True, slots=True)
class ControlSetupParticipantApplyEvidence:
    """Exact command-scoped evidence bundle for P3-D-6.7 planning."""

    script_apply: ControlScriptApplyEvidence | None = None
    character_assignment: ControlCharacterAssignmentEvidence | None = None
    player_replacement: ControlPlayerReplacementEvidence | None = None

    def __post_init__(self) -> None:
        expected_types = {
            "script_apply": ControlScriptApplyEvidence,
            "character_assignment": ControlCharacterAssignmentEvidence,
            "player_replacement": ControlPlayerReplacementEvidence,
        }
        for item in fields(self):
            value = getattr(self, item.name)
            if value is not None and not isinstance(value, expected_types[item.name]):
                raise ControlSetupParticipantEvidenceError(
                    f"{item.name} has invalid Setup / Participant evidence type"
                )

    def validate_bindings(
        self,
        *,
        game_id: str,
        session_id: str,
        observed_state_version: int,
    ) -> None:
        """Validate the scope/version of present evidence without requiring it."""

        _require_text("game_id", game_id)
        _require_text("session_id", session_id)
        _require_non_negative_int(
            "observed_state_version", observed_state_version
        )
        for name, evidence in self._present().items():
            if (evidence.game_id, evidence.session_id) != (game_id, session_id):
                raise ControlSetupParticipantEvidenceError(
                    f"{name} scope does not match Actor turn"
                )
            if evidence.observed_state_version != observed_state_version:
                raise ControlSetupParticipantEvidenceError(
                    f"{name} state version does not match Actor turn"
                )

    def validate_for_command(
        self,
        command_type: SessionCommandType,
        *,
        game_id: str,
        session_id: str,
        observed_state_version: int,
    ) -> None:
        """Require exactly the evidence owned by one command and fail closed."""

        self.validate_bindings(
            game_id=game_id,
            session_id=session_id,
            observed_state_version=observed_state_version,
        )
        if not isinstance(command_type, SessionCommandType):
            raise ControlSetupParticipantEvidenceError(
                "command_type must be a SessionCommandType"
            )
        required = {
            SessionCommandType.SET_SCRIPT: frozenset({"script_apply"}),
            SessionCommandType.ASSIGN_CHARACTER: frozenset(
                {"character_assignment"}
            ),
            SessionCommandType.REPLACE_PLAYER: frozenset({"player_replacement"}),
        }
        present = frozenset(self._present())
        expected = required.get(command_type, frozenset())
        if present != expected:
            raise ControlSetupParticipantEvidenceError(
                f"{command_type.value} requires Setup / Participant evidence "
                f"{sorted(expected)}"
            )
        for evidence in self._present().values():
            if evidence.validation_status is not SetupParticipantEvidenceStatus.VERIFIED:
                raise ControlSetupParticipantEvidenceError(
                    f"{command_type.value} evidence is not VERIFIED"
                )

    def validate_command_bindings(
        self,
        command_type: SessionCommandType,
        payload: SessionCommandPayload,
        *,
        authorization_reference: str,
        confirmation_reference: str | None,
        active_ownership_generation: int | None = None,
    ) -> None:
        """Bind present evidence to canonical intent and delivery governance."""

        _require_text("authorization_reference", authorization_reference)
        present = self._present()
        if not present:
            return

        if command_type is SessionCommandType.SET_SCRIPT:
            evidence = self.script_apply
            if evidence is None or not isinstance(payload, SetScriptPayload):
                raise ControlSetupParticipantEvidenceError(
                    "SET_SCRIPT evidence does not match canonical payload"
                )
            if (
                evidence.script_id != payload.script_id
                or evidence.manifest_reference != payload.manifest_reference
            ):
                raise ControlSetupParticipantEvidenceError(
                    "SET_SCRIPT evidence reference does not match command"
                )
        elif command_type is SessionCommandType.ASSIGN_CHARACTER:
            evidence = self.character_assignment
            if evidence is None or not isinstance(payload, AssignCharacterPayload):
                raise ControlSetupParticipantEvidenceError(
                    "ASSIGN_CHARACTER evidence does not match canonical payload"
                )
            if (
                evidence.participant_id != payload.participant_id
                or evidence.character_id != payload.character_id
                or (
                    payload.expected_binding_version is not None
                    and evidence.expected_binding_version
                    != payload.expected_binding_version
                )
                or evidence.authorization_reference != authorization_reference
            ):
                raise ControlSetupParticipantEvidenceError(
                    "ASSIGN_CHARACTER evidence binding does not match command"
                )
        elif command_type is SessionCommandType.REPLACE_PLAYER:
            evidence = self.player_replacement
            if evidence is None or not isinstance(payload, ReplacePlayerPayload):
                raise ControlSetupParticipantEvidenceError(
                    "REPLACE_PLAYER evidence does not match canonical payload"
                )
            if (
                evidence.old_participant_id != payload.old_participant_id
                or evidence.new_participant_id != payload.new_participant_id
                or evidence.old_expected_binding_version
                != payload.expected_binding_version
                or evidence.authorization_reference != authorization_reference
                or confirmation_reference is None
                or evidence.confirmation_reference != confirmation_reference
            ):
                raise ControlSetupParticipantEvidenceError(
                    "REPLACE_PLAYER evidence binding does not match command"
                )
        else:
            raise ControlSetupParticipantEvidenceError(
                "extraneous Setup / Participant evidence for command"
            )

        evidence = next(iter(present.values()))
        if evidence.ownership_generation != active_ownership_generation:
            raise ControlSetupParticipantEvidenceError(
                "evidence ownership generation does not match Actor turn"
            )

    def _present(self) -> dict[str, SetupParticipantEvidence]:
        return {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if getattr(self, item.name) is not None
        }


def _validate_common(value: object) -> None:
    contract_version = getattr(value, "contract_version")
    if (
        not isinstance(contract_version, int)
        or isinstance(contract_version, bool)
        or contract_version != 1
    ):
        raise ControlSetupParticipantEvidenceError(
            "unsupported Setup / Participant evidence contract version"
        )
    for name in ("game_id", "session_id"):
        _require_text(name, getattr(value, name))
    _require_non_negative_int(
        "observed_state_version", getattr(value, "observed_state_version")
    )
    if not isinstance(
        getattr(value, "validation_status"), SetupParticipantEvidenceStatus
    ):
        raise ControlSetupParticipantEvidenceError(
            "validation_status must be a SetupParticipantEvidenceStatus"
        )


def _require_single_step(name: str, expected: object, resulting: object) -> None:
    _require_non_negative_int(f"expected {name}", expected)
    _require_positive_int(f"resulting {name}", resulting)
    if resulting != expected + 1:
        raise ControlSetupParticipantEvidenceError(
            f"resulting {name} must advance expected {name} once"
        )


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ControlSetupParticipantEvidenceError(f"{name} must be non-empty text")


def _require_non_negative_int(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ControlSetupParticipantEvidenceError(
            f"{name} must be a non-negative integer"
        )


def _require_positive_int(name: str, value: object) -> None:
    _require_non_negative_int(name, value)
    if value == 0:
        raise ControlSetupParticipantEvidenceError(
            f"{name} must be a positive integer"
        )


def _require_optional_positive_int(name: str, value: object) -> None:
    if value is not None:
        _require_positive_int(name, value)
