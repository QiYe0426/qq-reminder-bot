"""Immutable Game Rule activation evidence for future pure plan building."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .commands import (
    ActivateRuleSetPayload,
    RevealCluePayload,
    SessionCommandPayload,
    SessionCommandType,
)


class ControlGameRuleEvidenceError(ValueError):
    """Raised when Game Rule evidence is malformed or inconsistently bound."""


class GameRuleEvidenceStatus(str, Enum):
    VERIFIED = "VERIFIED"
    UNKNOWN = "UNKNOWN"


class ClueRevealDisposition(str, Enum):
    AVAILABLE = "AVAILABLE"
    ALREADY_REVEALED = "ALREADY_REVEALED"
    NOT_FOUND = "NOT_FOUND"
    NOT_REVEALABLE = "NOT_REVEALABLE"
    RULE_SET_NOT_ACTIVE = "RULE_SET_NOT_ACTIVE"
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
    initial_disclosure_state_reference: str
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
            "initial_disclosure_state_reference",
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
class ControlClueRevealEvidence:
    evidence_schema_version: int
    game_id: str
    session_id: str
    observed_state_version: int
    clue_id: str
    active_rule_set_reference: str | None
    current_disclosure_state_reference: str | None
    resulting_disclosure_state_reference: str | None
    current_hidden_state_reference: str | None
    resulting_hidden_state_reference: str | None
    game_rule_version: int
    hidden_state_version: int
    public_disclosure_reference: str | None
    provenance_reference: str | None
    disposition: ClueRevealDisposition
    validation_status: GameRuleEvidenceStatus

    def __post_init__(self) -> None:
        if (
            not isinstance(self.evidence_schema_version, int)
            or isinstance(self.evidence_schema_version, bool)
            or self.evidence_schema_version != 1
        ):
            raise ControlGameRuleEvidenceError(
                "unsupported clue reveal evidence schema version"
            )
        for name in ("game_id", "session_id", "clue_id"):
            _require_text(name, getattr(self, name))
        for name in (
            "observed_state_version",
            "game_rule_version",
            "hidden_state_version",
        ):
            _require_non_negative_int(name, getattr(self, name))
        for name in (
            "active_rule_set_reference",
            "current_disclosure_state_reference",
            "resulting_disclosure_state_reference",
            "current_hidden_state_reference",
            "resulting_hidden_state_reference",
            "public_disclosure_reference",
            "provenance_reference",
        ):
            _require_optional_text(name, getattr(self, name))
        if not isinstance(self.disposition, ClueRevealDisposition):
            raise ControlGameRuleEvidenceError(
                "disposition must be a ClueRevealDisposition"
            )
        if self.validation_status is not GameRuleEvidenceStatus.VERIFIED:
            raise ControlGameRuleEvidenceError(
                "clue reveal evidence must be VERIFIED"
            )

        current_values = (
            self.active_rule_set_reference,
            self.current_disclosure_state_reference,
            self.current_hidden_state_reference,
        )
        result_values = (
            self.resulting_disclosure_state_reference,
            self.resulting_hidden_state_reference,
            self.public_disclosure_reference,
            self.provenance_reference,
        )
        if self.disposition is ClueRevealDisposition.RULE_SET_NOT_ACTIVE:
            if any(value is not None for value in current_values + result_values):
                raise ControlGameRuleEvidenceError(
                    "RULE_SET_NOT_ACTIVE evidence must not carry references"
                )
            if self.game_rule_version != 0 or self.hidden_state_version != 0:
                raise ControlGameRuleEvidenceError(
                    "RULE_SET_NOT_ACTIVE evidence requires zero domain versions"
                )
            return
        if any(value is None for value in current_values):
            raise ControlGameRuleEvidenceError(
                "active clue reveal evidence requires current references"
            )
        if self.game_rule_version == 0 or self.hidden_state_version == 0:
            raise ControlGameRuleEvidenceError(
                "active clue reveal evidence requires positive domain versions"
            )
        if self.disposition is ClueRevealDisposition.AVAILABLE:
            if any(value is None for value in result_values):
                raise ControlGameRuleEvidenceError(
                    "AVAILABLE evidence requires complete result references"
                )
            if (
                self.current_disclosure_state_reference
                == self.resulting_disclosure_state_reference
                or self.current_hidden_state_reference
                == self.resulting_hidden_state_reference
            ):
                raise ControlGameRuleEvidenceError(
                    "AVAILABLE evidence must advance committed references"
                )
            return
        if any(value is not None for value in result_values):
            raise ControlGameRuleEvidenceError(
                "non-available evidence must not carry result references"
            )


@dataclass(frozen=True, slots=True)
class ControlGameRuleApplyEvidence:
    """Exact command-scoped Game Rule evidence bundle."""

    rule_set_activation: ControlRuleSetActivationEvidence | None = None
    clue_reveal: ControlClueRevealEvidence | None = None

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
        if self.clue_reveal is not None and not isinstance(
            self.clue_reveal, ControlClueRevealEvidence
        ):
            raise ControlGameRuleEvidenceError(
                "clue_reveal has invalid Game Rule evidence type"
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
        if evidence is not None:
            if (evidence.game_id, evidence.session_id) != (game_id, session_id):
                raise ControlGameRuleEvidenceError(
                    "rule_set_activation scope does not match Actor turn"
                )
            if evidence.observed_state_version != observed_state_version:
                raise ControlGameRuleEvidenceError(
                    "rule_set_activation state version does not match Actor turn"
                )
        reveal = self.clue_reveal
        if reveal is not None:
            if (reveal.game_id, reveal.session_id) != (game_id, session_id):
                raise ControlGameRuleEvidenceError(
                    "clue_reveal scope does not match Actor turn"
                )
            if reveal.observed_state_version != observed_state_version:
                raise ControlGameRuleEvidenceError(
                    "clue_reveal state version does not match Actor turn"
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
            if activation is None or self.clue_reveal is not None:
                raise ControlGameRuleEvidenceError(
                    "ACTIVATE_RULE_SET requires rule_set_activation evidence"
                )
            if activation.validation_status is not GameRuleEvidenceStatus.VERIFIED:
                raise ControlGameRuleEvidenceError(
                    "ACTIVATE_RULE_SET evidence must be VERIFIED"
            )
            return
        if command_type is SessionCommandType.REVEAL_CLUE:
            reveal = self.clue_reveal
            if reveal is None or activation is not None:
                raise ControlGameRuleEvidenceError(
                    "REVEAL_CLUE requires clue_reveal evidence"
                )
            if reveal.disposition is ClueRevealDisposition.UNKNOWN:
                raise ControlGameRuleEvidenceError(
                    "REVEAL_CLUE evidence disposition is UNKNOWN"
                )
            return
        if activation is not None or self.clue_reveal is not None:
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
            if command_type is SessionCommandType.REVEAL_CLUE:
                reveal = self.clue_reveal
                if reveal is None or not isinstance(payload, RevealCluePayload):
                    raise ControlGameRuleEvidenceError(
                        "REVEAL_CLUE evidence does not match canonical payload"
                    )
                if (
                    reveal.clue_id != payload.clue_id
                    or reveal.game_rule_version
                    != payload.expected_game_rule_version
                    or reveal.hidden_state_version
                    != payload.expected_hidden_state_version
                ):
                    raise ControlGameRuleEvidenceError(
                        "REVEAL_CLUE evidence does not match command payload"
                    )
                return
            if evidence is not None or self.clue_reveal is not None:
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


def _require_optional_text(name: str, value: object) -> None:
    if value is not None:
        _require_text(name, value)
