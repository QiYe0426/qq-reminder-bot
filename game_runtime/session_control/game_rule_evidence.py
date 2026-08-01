"""Immutable Game Rule activation evidence for future pure plan building."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .commands import ActivateRuleSetPayload, SessionCommandPayload, SessionCommandType


class ControlGameRuleEvidenceError(ValueError):
    """Raised when Game Rule evidence is malformed or inconsistently bound."""


class GameRuleEvidenceStatus(str, Enum):
    VERIFIED = "VERIFIED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ControlRuleSetActivationEvidence:
    """Immutable observation required to activate one committed rule set."""

    evidence_schema_version: int
    game_id: str
    session_id: str
    observed_state_version: int
    setup_manifest_reference: str
    setup_version: int
    committed_rule_set_reference: str
    rule_set_version: int
    opaque_hidden_state_reference: str
    hidden_state_version: int
    provenance_reference: str
    validation_status: GameRuleEvidenceStatus

    def __post_init__(self) -> None:
        if (
            not isinstance(self.evidence_schema_version, int)
            or isinstance(self.evidence_schema_version, bool)
            or self.evidence_schema_version != 1
        ):
            raise ControlGameRuleEvidenceError(
                "unsupported Game Rule evidence schema version"
            )
        for name in (
            "game_id",
            "session_id",
            "setup_manifest_reference",
            "committed_rule_set_reference",
            "opaque_hidden_state_reference",
            "provenance_reference",
        ):
            _require_text(name, getattr(self, name))
        for name in (
            "observed_state_version",
            "setup_version",
            "rule_set_version",
            "hidden_state_version",
        ):
            _require_non_negative_int(name, getattr(self, name))
        if not isinstance(self.validation_status, GameRuleEvidenceStatus):
            raise ControlGameRuleEvidenceError(
                "validation_status must be a GameRuleEvidenceStatus"
            )


@dataclass(frozen=True, slots=True)
class ControlGameRuleApplyEvidence:
    """Exact command-scoped Game Rule evidence bundle."""

    rule_set_activation: ControlRuleSetActivationEvidence | None = None

    def __post_init__(self) -> None:
        if (
            self.rule_set_activation is not None
            and not isinstance(
                self.rule_set_activation, ControlRuleSetActivationEvidence
            )
        ):
            raise ControlGameRuleEvidenceError(
                "rule_set_activation has invalid Game Rule evidence type"
            )

    def validate_bindings(
        self,
        *,
        game_id: str,
        session_id: str,
        observed_state_version: int,
    ) -> None:
        """Validate present evidence without requiring command completeness."""

        _require_text("game_id", game_id)
        _require_text("session_id", session_id)
        _require_non_negative_int("observed_state_version", observed_state_version)
        evidence = self.rule_set_activation
        if evidence is None:
            return
        if (evidence.game_id, evidence.session_id) != (game_id, session_id):
            raise ControlGameRuleEvidenceError(
                "rule_set_activation scope does not match Actor turn"
            )
        if evidence.observed_state_version != observed_state_version:
            raise ControlGameRuleEvidenceError(
                "rule_set_activation state version does not match Actor turn"
            )

    def validate_for_command(
        self,
        command_type: SessionCommandType,
        *,
        game_id: str,
        session_id: str,
        observed_state_version: int,
    ) -> None:
        """Require the exact verified Evidence set for one supported command."""

        self.validate_bindings(
            game_id=game_id,
            session_id=session_id,
            observed_state_version=observed_state_version,
        )
        if not isinstance(command_type, SessionCommandType):
            raise ControlGameRuleEvidenceError(
                "command_type must be a SessionCommandType"
            )
        activation = self.rule_set_activation
        if command_type is SessionCommandType.ACTIVATE_RULE_SET:
            if activation is None:
                raise ControlGameRuleEvidenceError(
                    "ACTIVATE_RULE_SET requires rule_set_activation evidence"
                )
            if activation.validation_status is not GameRuleEvidenceStatus.VERIFIED:
                raise ControlGameRuleEvidenceError(
                    "ACTIVATE_RULE_SET evidence must be VERIFIED"
                )
            return
        if activation is not None:
            raise ControlGameRuleEvidenceError(
                f"{command_type.value} requires empty Game Rule evidence"
            )

    def validate_payload_binding(
        self,
        command_type: SessionCommandType,
        payload: SessionCommandPayload,
    ) -> None:
        """Bind activation evidence to every version in canonical command payload."""

        if not isinstance(command_type, SessionCommandType):
            raise ControlGameRuleEvidenceError(
                "command_type must be a SessionCommandType"
            )
        evidence = self.rule_set_activation
        if command_type is not SessionCommandType.ACTIVATE_RULE_SET:
            if evidence is not None:
                raise ControlGameRuleEvidenceError(
                    "extraneous Game Rule evidence for command"
                )
            return
        if evidence is None or not isinstance(payload, ActivateRuleSetPayload):
            raise ControlGameRuleEvidenceError(
                "ACTIVATE_RULE_SET evidence does not match canonical payload"
            )
        if (
            evidence.setup_manifest_reference != payload.manifest_reference
            or evidence.setup_version != payload.expected_setup_version
            or evidence.rule_set_version != payload.expected_game_rule_version
            or evidence.hidden_state_version != payload.expected_hidden_state_version
        ):
            raise ControlGameRuleEvidenceError(
                "ACTIVATE_RULE_SET evidence does not match command payload"
            )


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ControlGameRuleEvidenceError(f"{name} must be non-empty text")


def _require_non_negative_int(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ControlGameRuleEvidenceError(
            f"{name} must be a non-negative integer"
        )
