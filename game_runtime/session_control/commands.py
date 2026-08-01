"""Platform-neutral Session Control command contracts for P3-D-1."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from game_runtime.session import GamePhase


class SessionCommandType(str, Enum):
    """Closed set of commands accepted by the Session Control Plane."""

    CREATE_SESSION = "CREATE_SESSION"
    START_GAME = "START_GAME"
    PAUSE_GAME = "PAUSE_GAME"
    RESUME_GAME = "RESUME_GAME"
    END_GAME = "END_GAME"
    CHANGE_PHASE = "CHANGE_PHASE"
    SET_SCRIPT = "SET_SCRIPT"
    ASSIGN_CHARACTER = "ASSIGN_CHARACTER"
    REPLACE_PLAYER = "REPLACE_PLAYER"
    ACTIVATE_RULE_SET = "ACTIVATE_RULE_SET"


@dataclass(frozen=True, slots=True)
class SessionCommandPayload:
    """Marker base for closed, command-specific payload schemas."""


@dataclass(frozen=True, slots=True)
class CreateSessionPayload(SessionCommandPayload):
    prospective_dm_id: str

    def __post_init__(self) -> None:
        _require_non_empty("prospective_dm_id", self.prospective_dm_id)


@dataclass(frozen=True, slots=True)
class StartGamePayload(SessionCommandPayload):
    """START_GAME has no command-specific fields in P3-D-1."""


@dataclass(frozen=True, slots=True)
class PauseGamePayload(SessionCommandPayload):
    reason_code: str

    def __post_init__(self) -> None:
        _require_non_empty("reason_code", self.reason_code)


@dataclass(frozen=True, slots=True)
class ResumeGamePayload(SessionCommandPayload):
    """RESUME_GAME has no command-specific fields in P3-D-1."""


@dataclass(frozen=True, slots=True)
class EndGamePayload(SessionCommandPayload):
    public_result_reference: str | None = None

    def __post_init__(self) -> None:
        _require_optional_non_empty(
            "public_result_reference", self.public_result_reference
        )


@dataclass(frozen=True, slots=True)
class ChangePhasePayload(SessionCommandPayload):
    target_phase: GamePhase

    def __post_init__(self) -> None:
        if not isinstance(self.target_phase, GamePhase):
            raise TypeError("target_phase must be a GamePhase")


@dataclass(frozen=True, slots=True)
class SetScriptPayload(SessionCommandPayload):
    script_id: str
    public_name: str
    manifest_reference: str

    def __post_init__(self) -> None:
        _require_non_empty("script_id", self.script_id)
        _require_non_empty("public_name", self.public_name)
        _require_non_empty("manifest_reference", self.manifest_reference)


@dataclass(frozen=True, slots=True)
class AssignCharacterPayload(SessionCommandPayload):
    participant_id: str
    character_id: str
    expected_binding_version: int | None = None

    def __post_init__(self) -> None:
        _require_non_empty("participant_id", self.participant_id)
        _require_non_empty("character_id", self.character_id)
        _require_optional_non_negative(
            "expected_binding_version", self.expected_binding_version
        )


@dataclass(frozen=True, slots=True)
class ReplacePlayerPayload(SessionCommandPayload):
    old_participant_id: str
    new_participant_id: str
    expected_binding_version: int

    def __post_init__(self) -> None:
        _require_non_empty("old_participant_id", self.old_participant_id)
        _require_non_empty("new_participant_id", self.new_participant_id)
        if self.old_participant_id == self.new_participant_id:
            raise ValueError("replacement participants must be different")
        _require_non_negative(
            "expected_binding_version", self.expected_binding_version
        )


@dataclass(frozen=True, slots=True)
class ActivateRuleSetPayload(SessionCommandPayload):
    manifest_reference: str
    expected_setup_version: int
    expected_game_rule_version: int
    expected_hidden_state_version: int

    def __post_init__(self) -> None:
        _require_non_empty("manifest_reference", self.manifest_reference)
        _require_non_negative("expected_setup_version", self.expected_setup_version)
        _require_non_negative(
            "expected_game_rule_version", self.expected_game_rule_version
        )
        _require_non_negative(
            "expected_hidden_state_version", self.expected_hidden_state_version
        )


_PAYLOAD_TYPE_BY_COMMAND: dict[
    SessionCommandType, type[SessionCommandPayload]
] = {
    SessionCommandType.CREATE_SESSION: CreateSessionPayload,
    SessionCommandType.START_GAME: StartGamePayload,
    SessionCommandType.PAUSE_GAME: PauseGamePayload,
    SessionCommandType.RESUME_GAME: ResumeGamePayload,
    SessionCommandType.END_GAME: EndGamePayload,
    SessionCommandType.CHANGE_PHASE: ChangePhasePayload,
    SessionCommandType.SET_SCRIPT: SetScriptPayload,
    SessionCommandType.ASSIGN_CHARACTER: AssignCharacterPayload,
    SessionCommandType.REPLACE_PLAYER: ReplacePlayerPayload,
    SessionCommandType.ACTIVATE_RULE_SET: ActivateRuleSetPayload,
}


@dataclass(frozen=True, slots=True)
class SessionCommand:
    """Validated command envelope before resolve or authorization."""

    command_id: str
    command_type: SessionCommandType
    requester: str
    group_id: str
    game_id: str | None
    session_id: str | None
    requester_binding_version: int | None
    observed_state_version: int | None
    payload: SessionCommandPayload
    correlation_id: str
    requested_at: datetime

    def __post_init__(self) -> None:
        _require_non_empty("command_id", self.command_id)
        if not isinstance(self.command_type, SessionCommandType):
            raise TypeError("command_type must be a SessionCommandType")
        _require_non_empty("requester", self.requester)
        _require_non_empty("group_id", self.group_id)
        _require_non_empty("correlation_id", self.correlation_id)

        if not isinstance(self.requested_at, datetime):
            raise TypeError("requested_at must be a datetime")
        if (
            self.requested_at.tzinfo is None
            or self.requested_at.utcoffset() is None
        ):
            raise ValueError("requested_at must be timezone-aware")

        if not isinstance(self.payload, SessionCommandPayload):
            raise TypeError("payload must be a SessionCommandPayload")
        expected_payload_type = _PAYLOAD_TYPE_BY_COMMAND[self.command_type]
        if type(self.payload) is not expected_payload_type:
            raise TypeError(
                f"{self.command_type.value} payload must be "
                f"{expected_payload_type.__name__}"
            )

        if self.command_type is SessionCommandType.CREATE_SESSION:
            if self.game_id is not None or self.session_id is not None:
                raise ValueError(
                    "CREATE_SESSION must not carry game_id or session_id"
                )
            if self.observed_state_version is not None:
                raise ValueError(
                    "CREATE_SESSION must not carry observed_state_version"
                )
            if self.requester_binding_version is not None:
                raise ValueError(
                    "CREATE_SESSION must not carry requester_binding_version"
                )
            return

        _require_non_empty("game_id", self.game_id)
        _require_non_empty("session_id", self.session_id)
        if self.requester_binding_version is None:
            raise ValueError(
                "non-CREATE_SESSION command requires requester_binding_version"
            )
        _require_non_negative(
            "requester_binding_version", self.requester_binding_version
        )
        if self.observed_state_version is None:
            raise ValueError(
                "non-CREATE_SESSION command requires observed_state_version"
            )
        _require_non_negative(
            "observed_state_version", self.observed_state_version
        )


def _require_non_empty(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} must not be empty")


def _require_optional_non_empty(name: str, value: str | None) -> None:
    if value is not None:
        _require_non_empty(name, value)


def _require_non_negative(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must not be negative")


def _require_optional_non_negative(name: str, value: int | None) -> None:
    if value is not None:
        _require_non_negative(name, value)
