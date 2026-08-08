"""Immutable, command-bound evidence for Quest activation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from game_runtime.session_control.commands import (
    ActivateQuestPayload,
    SessionCommandType,
)


class ControlQuestEvidenceError(ValueError):
    """Raised when Quest evidence is malformed or inconsistently bound."""


class QuestEvidenceStatus(str, Enum):
    VERIFIED = "VERIFIED"
    UNKNOWN = "UNKNOWN"


class QuestActivationDisposition(str, Enum):
    AVAILABLE = "AVAILABLE"
    ALREADY_ACTIVE = "ALREADY_ACTIVE"
    CONFLICT = "CONFLICT"
    NOT_FOUND = "NOT_FOUND"
    NOT_ACTIVATABLE = "NOT_ACTIVATABLE"
    RULE_SET_NOT_ACTIVE = "RULE_SET_NOT_ACTIVE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ControlQuestActivationEvidence:
    evidence_schema_version: int
    game_id: str
    session_id: str
    observed_state_version: int
    quest_id: str
    active_rule_set_reference: str | None
    current_active_quest_id: str | None
    current_public_state_reference: str | None
    resulting_public_state_reference: str | None
    current_hidden_state_reference: str | None
    resulting_hidden_state_reference: str | None
    game_rule_version: int
    quest_version: int
    hidden_state_version: int
    provenance_reference: str | None
    disposition: QuestActivationDisposition
    validation_status: QuestEvidenceStatus

    def __post_init__(self) -> None:
        if (
            not isinstance(self.evidence_schema_version, int)
            or isinstance(self.evidence_schema_version, bool)
            or self.evidence_schema_version != 1
        ):
            raise ControlQuestEvidenceError(
                "unsupported Quest evidence schema version"
            )
        for name in ("game_id", "session_id", "quest_id"):
            _require_text(name, getattr(self, name))
        for name in (
            "observed_state_version",
            "game_rule_version",
            "quest_version",
            "hidden_state_version",
        ):
            _require_non_negative_int(name, getattr(self, name))
        for name in (
            "active_rule_set_reference",
            "current_active_quest_id",
            "current_public_state_reference",
            "resulting_public_state_reference",
            "current_hidden_state_reference",
            "resulting_hidden_state_reference",
            "provenance_reference",
        ):
            _require_optional_text(name, getattr(self, name))
        if not isinstance(self.disposition, QuestActivationDisposition):
            raise ControlQuestEvidenceError(
                "disposition must be a QuestActivationDisposition"
            )
        if self.validation_status is not QuestEvidenceStatus.VERIFIED:
            raise ControlQuestEvidenceError(
                "Quest activation evidence must be VERIFIED"
            )
        self._validate_exact_set()

    def _validate_exact_set(self) -> None:
        if self.disposition is QuestActivationDisposition.RULE_SET_NOT_ACTIVE:
            if any(
                value is not None
                for value in (
                    self.active_rule_set_reference,
                    self.current_active_quest_id,
                    self.current_public_state_reference,
                    self.resulting_public_state_reference,
                    self.current_hidden_state_reference,
                    self.resulting_hidden_state_reference,
                    self.provenance_reference,
                )
            ) or any(
                (self.game_rule_version, self.quest_version, self.hidden_state_version)
            ):
                raise ControlQuestEvidenceError(
                    "RULE_SET_NOT_ACTIVE evidence must be empty"
                )
            return

        if (
            self.active_rule_set_reference is None
            or self.current_hidden_state_reference is None
            or self.game_rule_version == 0
            or self.hidden_state_version == 0
        ):
            raise ControlQuestEvidenceError(
                "active rule-set evidence requires current bindings"
            )
        current_pair = (
            self.current_active_quest_id,
            self.current_public_state_reference,
        )
        if (current_pair[0] is None) != (current_pair[1] is None):
            raise ControlQuestEvidenceError(
                "current Quest evidence must be empty or complete"
            )
        current_active = current_pair[0] is not None
        if current_active != (self.quest_version > 0):
            raise ControlQuestEvidenceError(
                "current Quest evidence does not match Quest version"
            )

        result_values = (
            self.resulting_public_state_reference,
            self.resulting_hidden_state_reference,
            self.provenance_reference,
        )
        if self.disposition is QuestActivationDisposition.AVAILABLE:
            if current_active or self.quest_version != 0:
                raise ControlQuestEvidenceError(
                    "AVAILABLE evidence requires an empty Quest slice"
                )
            if any(value is None for value in result_values):
                raise ControlQuestEvidenceError(
                    "AVAILABLE evidence requires complete result references"
                )
            if (
                self.current_hidden_state_reference
                == self.resulting_hidden_state_reference
            ):
                raise ControlQuestEvidenceError(
                    "AVAILABLE evidence must advance hidden state"
                )
            return
        if any(value is not None for value in result_values):
            raise ControlQuestEvidenceError(
                "non-available evidence must not carry result references"
            )
        if self.disposition in {
            QuestActivationDisposition.ALREADY_ACTIVE,
            QuestActivationDisposition.CONFLICT,
        } and not current_active:
            raise ControlQuestEvidenceError(
                "active Quest disposition requires current Quest state"
            )
        if self.disposition in {
            QuestActivationDisposition.NOT_FOUND,
            QuestActivationDisposition.NOT_ACTIVATABLE,
            QuestActivationDisposition.UNKNOWN,
        } and current_active:
            raise ControlQuestEvidenceError(
                "inactive Quest disposition requires an empty Quest slice"
            )

    def validate_for_command(
        self,
        command_type: SessionCommandType,
        *,
        game_id: str,
        session_id: str,
        observed_state_version: int,
    ) -> None:
        if command_type is not SessionCommandType.ACTIVATE_QUEST:
            raise ControlQuestEvidenceError(
                "Quest activation evidence is extraneous for command"
            )
        self.validate_bindings(
            game_id=game_id,
            session_id=session_id,
            observed_state_version=observed_state_version,
        )
        if self.disposition is QuestActivationDisposition.UNKNOWN:
            raise ControlQuestEvidenceError(
                "Quest activation evidence disposition is UNKNOWN"
            )

    def validate_bindings(
        self,
        *,
        game_id: str,
        session_id: str,
        observed_state_version: int,
    ) -> None:
        if (self.game_id, self.session_id) != (game_id, session_id):
            raise ControlQuestEvidenceError(
                "Quest activation evidence scope does not match Actor turn"
            )
        if self.observed_state_version != observed_state_version:
            raise ControlQuestEvidenceError(
                "Quest activation evidence version does not match Actor turn"
            )

    def validate_payload_binding(self, payload: ActivateQuestPayload) -> None:
        if not isinstance(payload, ActivateQuestPayload):
            raise ControlQuestEvidenceError(
                "Quest activation evidence requires ActivateQuestPayload"
            )
        if (
            self.quest_id != payload.quest_id
            or self.game_rule_version != payload.expected_game_rule_version
            or self.quest_version != payload.expected_quest_version
            or self.hidden_state_version != payload.expected_hidden_state_version
        ):
            raise ControlQuestEvidenceError(
                "Quest activation evidence does not match command payload"
            )


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ControlQuestEvidenceError(f"{name} must be non-empty text")


def _require_optional_text(name: str, value: object) -> None:
    if value is not None:
        _require_text(name, value)


def _require_non_negative_int(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ControlQuestEvidenceError(
            f"{name} must be a non-negative integer"
        )
