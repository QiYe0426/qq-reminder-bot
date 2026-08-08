"""Pure Atomic Apply contracts for Session Control; no adapter or execution."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from game_runtime.event import (
    GameEvent,
    GameEventType,
    SessionControlRejectedPayload,
    validate_control_result_event,
)
from game_runtime.participant import ParticipantType
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control.commands import SessionCommandType
from game_runtime.session_control.operation import ControlOperationStatus
from game_runtime.session_control.lifecycle_evidence import (
    ControlLifecycleApplyEvidence,
    ControlLifecycleEvidenceError,
)
from game_runtime.session_control.setup_participant_evidence import (
    ControlSetupParticipantApplyEvidence,
    ControlSetupParticipantEvidenceError,
)
from game_runtime.session_control.game_rule_evidence import (
    ControlGameRuleApplyEvidence,
    ControlGameRuleEvidenceError,
)


_RESULT_EVENT_TYPES_BY_COMMAND: dict[
    SessionCommandType, tuple[GameEventType, ...]
] = {
    SessionCommandType.START_GAME: (
        GameEventType.SESSION_STARTED,
        GameEventType.PHASE_CHANGED,
    ),
    SessionCommandType.PAUSE_GAME: (GameEventType.SESSION_PAUSED,),
    SessionCommandType.RESUME_GAME: (GameEventType.SESSION_RESUMED,),
    SessionCommandType.END_GAME: (GameEventType.SESSION_ENDED,),
    SessionCommandType.CHANGE_PHASE: (GameEventType.PHASE_CHANGED,),
    SessionCommandType.SET_SCRIPT: (GameEventType.SCRIPT_SET,),
    SessionCommandType.ASSIGN_CHARACTER: (GameEventType.CHARACTER_ASSIGNED,),
    SessionCommandType.REPLACE_PLAYER: (GameEventType.PLAYER_REPLACED,),
    SessionCommandType.ACTIVATE_RULE_SET: (GameEventType.RULE_SET_ACTIVATED,),
    SessionCommandType.REVEAL_CLUE: (GameEventType.CLUE_REVEALED,),
}


class ParticipantMutationType(str, Enum):
    UPSERT = "UPSERT"
    ASSIGN_CHARACTER = "ASSIGN_CHARACTER"
    REPLACE = "REPLACE"


@dataclass(frozen=True, slots=True)
class ParticipantMutation:
    mutation_type: ParticipantMutationType
    participant_id: str
    expected_binding_version: int | None
    resulting_binding_version: int
    participant_type: ParticipantType | None = None
    character_id: str | None = None
    replacement_participant_id: str | None = None
    old_expected_binding_version: int | None = None
    old_resulting_binding_version: int | None = None
    new_expected_binding_version: int | None = None
    new_resulting_binding_version: int | None = None
    character_binding_reference: str | None = None
    old_character_binding_reference: str | None = None
    new_character_binding_reference: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mutation_type, ParticipantMutationType):
            raise TypeError("mutation_type must be a ParticipantMutationType")
        _require_text("participant_id", self.participant_id)
        _require_optional_non_negative(
            "expected_binding_version", self.expected_binding_version
        )
        _require_positive("resulting_binding_version", self.resulting_binding_version)
        _require_optional_text("character_id", self.character_id)
        _require_optional_text(
            "replacement_participant_id", self.replacement_participant_id
        )
        for name in (
            "character_binding_reference",
            "old_character_binding_reference",
            "new_character_binding_reference",
        ):
            _require_optional_text(name, getattr(self, name))
        bilateral_versions = (
            self.old_expected_binding_version,
            self.old_resulting_binding_version,
            self.new_expected_binding_version,
            self.new_resulting_binding_version,
        )
        for name, value in zip(
            (
                "old_expected_binding_version",
                "old_resulting_binding_version",
                "new_expected_binding_version",
                "new_resulting_binding_version",
            ),
            bilateral_versions,
        ):
            _require_optional_non_negative(name, value)

        if self.mutation_type is ParticipantMutationType.UPSERT:
            if not isinstance(self.participant_type, ParticipantType):
                raise ValueError("UPSERT requires participant_type")
            if self.replacement_participant_id is not None:
                raise ValueError("UPSERT cannot replace another participant")
            if any(
                value is not None
                for value in (
                    self.character_binding_reference,
                    self.old_character_binding_reference,
                    self.new_character_binding_reference,
                )
            ):
                raise ValueError("UPSERT cannot carry character binding references")
        elif self.mutation_type is ParticipantMutationType.ASSIGN_CHARACTER:
            if (
                self.expected_binding_version is None
                or self.character_id is None
                or self.character_binding_reference is None
            ):
                raise ValueError(
                    "ASSIGN_CHARACTER requires binding, character, and reference evidence"
                )
            if (
                self.participant_type is not None
                or self.replacement_participant_id is not None
            ):
                raise ValueError(
                    "ASSIGN_CHARACTER cannot change participant identity"
                )
            if (
                self.old_character_binding_reference is not None
                or self.new_character_binding_reference is not None
            ):
                raise ValueError("ASSIGN_CHARACTER cannot carry replacement references")
        elif self.mutation_type is ParticipantMutationType.REPLACE:
            if (
                self.expected_binding_version is None
                or self.replacement_participant_id is None
            ):
                raise ValueError("REPLACE requires old binding and replacement")
            if self.replacement_participant_id == self.participant_id:
                raise ValueError("replacement participant must be different")
            if self.participant_type is not None or self.character_id is not None:
                raise ValueError("REPLACE cannot embed participant or character data")
            if self.character_binding_reference is not None:
                raise ValueError("REPLACE uses old/new character binding references")
            if any(value is None for value in bilateral_versions):
                raise ValueError("REPLACE requires bilateral binding versions")
            if self.old_resulting_binding_version != self.old_expected_binding_version + 1:
                raise ValueError("old replacement binding must advance once")
            if self.new_resulting_binding_version != self.new_expected_binding_version + 1:
                raise ValueError("new replacement binding must advance once")
            if self.expected_binding_version != self.old_expected_binding_version:
                raise ValueError("REPLACE expected binding must equal old binding")
            if self.resulting_binding_version != self.new_resulting_binding_version:
                raise ValueError("REPLACE resulting binding must equal new binding")
            old_ref = self.old_character_binding_reference
            new_ref = self.new_character_binding_reference
            if (old_ref is None) != (new_ref is None) or old_ref != new_ref:
                raise ValueError(
                    "REPLACE must transfer one character binding reference or neither"
                )
        if (
            self.mutation_type is not ParticipantMutationType.REPLACE
            and any(value is not None for value in bilateral_versions)
        ):
            raise ValueError("bilateral binding versions are REPLACE-only")


class SetupMutationType(str, Enum):
    SET_SCRIPT = "SET_SCRIPT"


@dataclass(frozen=True, slots=True)
class SetupMutation:
    mutation_type: SetupMutationType
    script_id: str
    public_name: str
    manifest_reference: str
    manifest_version: int
    expected_setup_version: int
    resulting_setup_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.mutation_type, SetupMutationType):
            raise TypeError("mutation_type must be a SetupMutationType")
        for name in ("script_id", "public_name", "manifest_reference"):
            _require_text(name, getattr(self, name))
        _require_non_negative("manifest_version", self.manifest_version)
        _require_non_negative("expected_setup_version", self.expected_setup_version)
        _require_positive("resulting_setup_version", self.resulting_setup_version)
        if self.resulting_setup_version != self.expected_setup_version + 1:
            raise ValueError("resulting_setup_version must advance once")


class GameRuleMutationType(str, Enum):
    ACTIVATE_RULE_SET = "ACTIVATE_RULE_SET"
    REVEAL_CLUE = "REVEAL_CLUE"


@dataclass(frozen=True, slots=True)
class GameRuleMutation:
    mutation_type: GameRuleMutationType
    manifest_reference: str
    committed_rule_set_reference: str
    committed_disclosure_state_reference: str
    opaque_hidden_state_reference: str
    expected_game_rule_version: int
    resulting_game_rule_version: int
    expected_hidden_state_version: int
    resulting_hidden_state_version: int
    provenance_reference: str

    def __post_init__(self) -> None:
        if not isinstance(self.mutation_type, GameRuleMutationType):
            raise TypeError("mutation_type must be a GameRuleMutationType")
        if self.mutation_type is not GameRuleMutationType.ACTIVATE_RULE_SET:
            raise ValueError("GameRuleMutation only supports ACTIVATE_RULE_SET")
        for name in (
            "manifest_reference",
            "committed_rule_set_reference",
            "committed_disclosure_state_reference",
            "opaque_hidden_state_reference",
            "provenance_reference",
        ):
            _require_text(name, getattr(self, name))
        for name in (
            "expected_game_rule_version",
            "expected_hidden_state_version",
        ):
            _require_non_negative(name, getattr(self, name))
        for name in (
            "resulting_game_rule_version",
            "resulting_hidden_state_version",
        ):
            _require_positive(name, getattr(self, name))
        if (
            self.resulting_game_rule_version
            != self.expected_game_rule_version + 1
        ):
            raise ValueError("resulting_game_rule_version must advance once")
        if (
            self.resulting_hidden_state_version
            != self.expected_hidden_state_version + 1
        ):
            raise ValueError("resulting_hidden_state_version must advance once")


@dataclass(frozen=True, slots=True)
class ClueRevealMutation:
    mutation_type: GameRuleMutationType
    clue_id: str
    active_rule_set_reference: str
    public_disclosure_reference: str
    current_disclosure_state_reference: str
    resulting_disclosure_state_reference: str
    current_hidden_state_reference: str
    resulting_hidden_state_reference: str
    expected_game_rule_version: int
    resulting_game_rule_version: int
    expected_hidden_state_version: int
    resulting_hidden_state_version: int
    provenance_reference: str

    def __post_init__(self) -> None:
        if self.mutation_type is not GameRuleMutationType.REVEAL_CLUE:
            raise ValueError("ClueRevealMutation only supports REVEAL_CLUE")
        for name in (
            "clue_id",
            "active_rule_set_reference",
            "public_disclosure_reference",
            "current_disclosure_state_reference",
            "resulting_disclosure_state_reference",
            "current_hidden_state_reference",
            "resulting_hidden_state_reference",
            "provenance_reference",
        ):
            _require_text(name, getattr(self, name))
        for name in ("expected_game_rule_version", "expected_hidden_state_version"):
            _require_non_negative(name, getattr(self, name))
        for name in (
            "resulting_game_rule_version",
            "resulting_hidden_state_version",
        ):
            _require_positive(name, getattr(self, name))
        if self.resulting_game_rule_version != self.expected_game_rule_version + 1:
            raise ValueError("resulting_game_rule_version must advance once")
        if self.resulting_hidden_state_version != self.expected_hidden_state_version + 1:
            raise ValueError("resulting_hidden_state_version must advance once")
        if (
            self.current_disclosure_state_reference
            == self.resulting_disclosure_state_reference
            or self.current_hidden_state_reference
            == self.resulting_hidden_state_reference
        ):
            raise ValueError("reveal mutation must advance committed references")


class OwnershipIntentType(str, Enum):
    UNCHANGED = "UNCHANGED"
    ACQUIRE = "ACQUIRE"
    RETAIN = "RETAIN"
    RELEASE = "RELEASE"


@dataclass(frozen=True, slots=True)
class OwnershipIntent:
    intent_type: OwnershipIntentType
    expected_generation: int | None
    resulting_generation: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.intent_type, OwnershipIntentType):
            raise TypeError("intent_type must be an OwnershipIntentType")
        _require_optional_positive("expected_generation", self.expected_generation)
        _require_optional_positive("resulting_generation", self.resulting_generation)
        if self.intent_type is OwnershipIntentType.ACQUIRE:
            if self.expected_generation is not None:
                raise ValueError("ACQUIRE cannot expect active ownership")
            if self.resulting_generation is None:
                raise ValueError("ACQUIRE requires resulting_generation")
        elif self.intent_type is OwnershipIntentType.RELEASE:
            if self.expected_generation is None:
                raise ValueError("RELEASE requires expected_generation")
            if self.resulting_generation is not None:
                raise ValueError("RELEASE cannot retain ownership")
        elif self.intent_type in {
            OwnershipIntentType.UNCHANGED,
            OwnershipIntentType.RETAIN,
        }:
            if self.expected_generation != self.resulting_generation:
                raise ValueError(
                    f"{self.intent_type.value} must preserve ownership generation"
                )


@dataclass(frozen=True, slots=True)
class CandidateSessionSnapshot:
    game_id: str
    session_id: str
    group_id: str
    dm_participant_id: str
    status: GameSessionStatus
    current_phase: GamePhase
    state_version: int
    last_applied_sequence_no: int

    def __post_init__(self) -> None:
        for name in ("game_id", "session_id", "group_id", "dm_participant_id"):
            _require_text(name, getattr(self, name))
        if not isinstance(self.status, GameSessionStatus):
            raise TypeError("status must be a GameSessionStatus")
        if not isinstance(self.current_phase, GamePhase):
            raise TypeError("current_phase must be a GamePhase")
        _require_non_negative("state_version", self.state_version)
        _require_non_negative(
            "last_applied_sequence_no", self.last_applied_sequence_no
        )
        if (
            self.status is GameSessionStatus.CREATED
            and self.current_phase is not GamePhase.LOBBY
        ):
            raise ValueError("CREATED candidate must be in LOBBY")
        if (
            self.status is GameSessionStatus.RUNNING
            and self.current_phase is GamePhase.LOBBY
        ):
            raise ValueError("RUNNING candidate cannot be in LOBBY")
        if (
            self.status is GameSessionStatus.ENDED
            and self.current_phase is not GamePhase.ENDING
        ):
            raise ValueError("ENDED candidate must be in ENDING")


@dataclass(frozen=True, slots=True)
class ControlOperationClaim:
    game_id: str
    session_id: str
    command_id: str
    operation_id: str
    input_event_id: str
    claim_id: str
    claimed_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "game_id",
            "session_id",
            "command_id",
            "operation_id",
            "input_event_id",
            "claim_id",
        ):
            _require_text(name, getattr(self, name))
        _require_time("claimed_at", self.claimed_at)


@dataclass(frozen=True, slots=True)
class ControlApplyPlan:
    game_id: str
    session_id: str
    group_id: str
    command_id: str
    command_type: SessionCommandType
    operation_id: str
    operation_claim_id: str
    input_event_id: str
    input_sequence_no: int
    expected_state_version: int
    expected_cursor: int
    expected_binding_version: int | None
    candidate_snapshot: CandidateSessionSnapshot
    participant_mutations: tuple[ParticipantMutation, ...]
    setup_mutations: tuple[SetupMutation, ...]
    ownership_intent: OwnershipIntent
    result_events: tuple[GameEvent, ...]
    operation_terminal_state: ControlOperationStatus
    lifecycle_evidence: ControlLifecycleApplyEvidence = field(
        default_factory=ControlLifecycleApplyEvidence
    )
    setup_participant_evidence: ControlSetupParticipantApplyEvidence = field(
        default_factory=ControlSetupParticipantApplyEvidence
    )
    game_rule_mutations: tuple[GameRuleMutation | ClueRevealMutation, ...] = ()
    game_rule_evidence: ControlGameRuleApplyEvidence = field(
        default_factory=ControlGameRuleApplyEvidence
    )

    def __post_init__(self) -> None:
        _validate_common_plan_fields(self)
        if not isinstance(self.candidate_snapshot, CandidateSessionSnapshot):
            raise TypeError("candidate_snapshot must be a CandidateSessionSnapshot")
        _freeze_typed_tuple(
            self,
            "participant_mutations",
            self.participant_mutations,
            ParticipantMutation,
        )
        _freeze_typed_tuple(
            self,
            "setup_mutations",
            self.setup_mutations,
            SetupMutation,
        )
        if not isinstance(self.ownership_intent, OwnershipIntent):
            raise TypeError("ownership_intent must be an OwnershipIntent")
        if not isinstance(self.lifecycle_evidence, ControlLifecycleApplyEvidence):
            raise TypeError(
                "lifecycle_evidence must be a ControlLifecycleApplyEvidence"
            )
        try:
            self.lifecycle_evidence.validate_for_command(
                self.command_type,
                game_id=self.game_id,
                session_id=self.session_id,
                observed_state_version=self.expected_state_version,
            )
        except ControlLifecycleEvidenceError as exc:
            raise ValueError(str(exc)) from exc
        if not isinstance(
            self.setup_participant_evidence,
            ControlSetupParticipantApplyEvidence,
        ):
            raise TypeError(
                "setup_participant_evidence must be a "
                "ControlSetupParticipantApplyEvidence"
            )
        try:
            self.setup_participant_evidence.validate_for_command(
                self.command_type,
                game_id=self.game_id,
                session_id=self.session_id,
                observed_state_version=self.expected_state_version,
            )
            _validate_setup_participant_mutations(self)
        except ControlSetupParticipantEvidenceError as exc:
            raise ValueError(str(exc)) from exc
        _freeze_typed_tuple(
            self,
            "game_rule_mutations",
            self.game_rule_mutations,
            (GameRuleMutation, ClueRevealMutation),
        )
        if not isinstance(self.game_rule_evidence, ControlGameRuleApplyEvidence):
            raise TypeError(
                "game_rule_evidence must be a ControlGameRuleApplyEvidence"
            )
        try:
            self.game_rule_evidence.validate_for_command(
                self.command_type,
                game_id=self.game_id,
                session_id=self.session_id,
                observed_state_version=self.expected_state_version,
            )
            _validate_game_rule_mutations(self)
        except ControlGameRuleEvidenceError as exc:
            raise ValueError(str(exc)) from exc
        result_events = _freeze_result_events(self, self.result_events)
        if self.operation_terminal_state is not ControlOperationStatus.SUCCESS:
            raise ValueError("ControlApplyPlan terminal state must be SUCCESS")
        if (
            self.candidate_snapshot.game_id != self.game_id
            or self.candidate_snapshot.session_id != self.session_id
            or self.candidate_snapshot.group_id != self.group_id
        ):
            raise ValueError("candidate Snapshot scope does not match Apply Plan")
        if self.candidate_snapshot.state_version != self.expected_state_version + 1:
            raise ValueError(
                "candidate state_version must advance expected_state_version once"
            )
        if self.candidate_snapshot.last_applied_sequence_no != self.input_sequence_no:
            raise ValueError("candidate cursor must include the input Event")
        expected_event_types = _RESULT_EVENT_TYPES_BY_COMMAND.get(self.command_type)
        if expected_event_types is None:
            raise ValueError(
                "CREATE_SESSION must use create_session_with_event bootstrap"
            )
        if tuple(event.event_type for event in result_events) != expected_event_types:
            raise ValueError("result Event types do not match the Session Command")
        _validate_result_events(self, result_events, self.candidate_snapshot.state_version)


@dataclass(frozen=True, slots=True)
class ControlRejectPlan:
    game_id: str
    session_id: str
    group_id: str
    command_id: str
    command_type: SessionCommandType
    operation_id: str
    operation_claim_id: str
    input_event_id: str
    input_sequence_no: int
    expected_state_version: int
    expected_cursor: int
    expected_binding_version: int | None
    rejection_event: GameEvent
    operation_terminal_state: ControlOperationStatus

    def __post_init__(self) -> None:
        _validate_common_plan_fields(self)
        if self.operation_terminal_state not in {
            ControlOperationStatus.FAILED,
            ControlOperationStatus.CANCELLED,
        }:
            raise ValueError("Reject terminal state must be FAILED or CANCELLED")
        if not isinstance(self.rejection_event, GameEvent):
            raise TypeError("rejection_event must be a GameEvent")
        if self.rejection_event.event_type is not GameEventType.SESSION_CONTROL_REJECTED:
            raise ValueError("Reject Plan requires SESSION_CONTROL_REJECTED")
        payload = validate_control_result_event(self.rejection_event)
        if not isinstance(payload, SessionControlRejectedPayload):
            raise ValueError("Reject Event payload type does not match")
        _validate_one_result_event(self, self.rejection_event, self.expected_state_version)


@dataclass(frozen=True, slots=True)
class CommittedResultEventReference:
    """Stored identity evidence for one Result Event committed atomically."""

    event_id: str
    sequence_no: int
    event_type: GameEventType
    stored_event_reference: str

    def __post_init__(self) -> None:
        _require_text("event_id", self.event_id)
        _require_positive("sequence_no", self.sequence_no)
        if not isinstance(self.event_type, GameEventType):
            raise TypeError("event_type must be a GameEventType")
        _require_text("stored_event_reference", self.stored_event_reference)
        if self.stored_event_reference != self.event_id:
            raise ValueError("stored_event_reference must equal event_id")


@dataclass(frozen=True, slots=True)
class ControlApplyReceipt:
    game_id: str
    session_id: str
    committed_state_version: int
    committed_cursor: int
    result_event_ids: tuple[str, ...]
    operation_status: ControlOperationStatus
    ownership_generation: int | None
    commit_evidence_reference: str
    command_id: str | None = None
    operation_id: str | None = None
    operation_claim_id: str | None = None
    input_event_id: str | None = None
    input_sequence_no: int | None = None
    result_event_references: tuple[CommittedResultEventReference, ...] = ()

    def __post_init__(self) -> None:
        _require_text("game_id", self.game_id)
        _require_text("session_id", self.session_id)
        _require_non_negative(
            "committed_state_version", self.committed_state_version
        )
        _require_non_negative("committed_cursor", self.committed_cursor)
        event_ids = tuple(self.result_event_ids)
        if not event_ids:
            raise ValueError("result_event_ids must not be empty")
        for event_id in event_ids:
            _require_text("result_event_id", event_id)
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("result_event_ids must be unique")
        object.__setattr__(self, "result_event_ids", event_ids)
        if self.operation_status not in {
            ControlOperationStatus.SUCCESS,
            ControlOperationStatus.FAILED,
            ControlOperationStatus.CANCELLED,
        }:
            raise ValueError("receipt requires a committed terminal Operation status")
        _require_optional_positive(
            "ownership_generation", self.ownership_generation
        )
        _require_text("commit_evidence_reference", self.commit_evidence_reference)
        identity_values = (
            self.command_id,
            self.operation_id,
            self.operation_claim_id,
            self.input_event_id,
            self.input_sequence_no,
        )
        if any(value is not None for value in identity_values):
            if any(value is None for value in identity_values):
                raise ValueError("receipt identity evidence must be complete")
            for name in (
                "command_id",
                "operation_id",
                "operation_claim_id",
                "input_event_id",
            ):
                _require_text(name, getattr(self, name))
            _require_positive("input_sequence_no", self.input_sequence_no)

        if not isinstance(self.result_event_references, (tuple, list)):
            raise TypeError("result_event_references must be a tuple or list")
        references = tuple(self.result_event_references)
        if any(
            not isinstance(reference, CommittedResultEventReference)
            for reference in references
        ):
            raise TypeError(
                "result_event_references contains an invalid reference"
            )
        if references:
            reference_ids = tuple(reference.event_id for reference in references)
            if reference_ids != event_ids:
                raise ValueError(
                    "result_event_references must match result_event_ids in order"
                )
            sequences = tuple(reference.sequence_no for reference in references)
            if any(
                current <= previous
                for previous, current in zip(sequences, sequences[1:])
            ):
                raise ValueError(
                    "result Event reference sequences must be strictly increasing"
                )
        object.__setattr__(self, "result_event_references", references)


@runtime_checkable
class CreateSessionWithEventPlan(Protocol):
    """Structural bootstrap contract; P3-D-6.4 supplies its immutable model."""

    @property
    def game_id(self) -> str: ...

    @property
    def session_id(self) -> str: ...

    @property
    def group_id(self) -> str: ...

    @property
    def command_id(self) -> str: ...

    @property
    def operation_id(self) -> str: ...

    @property
    def input_event_id(self) -> str: ...

    @property
    def operation_claim_id(self) -> str: ...

    @property
    def candidate_snapshot(self) -> CandidateSessionSnapshot: ...

    @property
    def initial_dm_participant(self) -> ParticipantMutation: ...

    @property
    def input_event(self) -> GameEvent: ...

    @property
    def result_event(self) -> GameEvent: ...

    @property
    def ownership_intent(self) -> OwnershipIntent: ...

    @property
    def operation_terminal_state(self) -> ControlOperationStatus: ...


class ControlApplyConflictReason(str, Enum):
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    OPERATION_CLAIM_MISMATCH = "OPERATION_CLAIM_MISMATCH"
    INPUT_EVENT_MISMATCH = "INPUT_EVENT_MISMATCH"
    STATE_VERSION_MISMATCH = "STATE_VERSION_MISMATCH"
    CURSOR_MISMATCH = "CURSOR_MISMATCH"
    BINDING_VERSION_MISMATCH = "BINDING_VERSION_MISMATCH"
    OWNERSHIP_MISMATCH = "OWNERSHIP_MISMATCH"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"


class ControlApplyContractError(RuntimeError):
    """Base failure exposed by the Atomic Apply Port."""


class ControlApplyConflict(ControlApplyContractError):
    """Typed optimistic-concurrency conflict; no commit occurred."""

    def __init__(
        self,
        reason: ControlApplyConflictReason,
        *,
        expected: object = None,
        actual: object = None,
    ) -> None:
        if not isinstance(reason, ControlApplyConflictReason):
            raise TypeError("reason must be a ControlApplyConflictReason")
        self.reason = reason
        self.expected = expected
        self.actual = actual
        super().__init__(reason.value)


class ControlApplyStorageFailure(ControlApplyContractError):
    """Non-deterministic storage failure; callers must fail closed."""


def _validate_common_plan_fields(plan: object) -> None:
    for name in (
        "game_id",
        "session_id",
        "group_id",
        "command_id",
        "operation_id",
        "operation_claim_id",
        "input_event_id",
    ):
        _require_text(name, getattr(plan, name))
    if not isinstance(getattr(plan, "command_type"), SessionCommandType):
        raise TypeError("command_type must be a SessionCommandType")
    _require_positive("input_sequence_no", getattr(plan, "input_sequence_no"))
    _require_non_negative(
        "expected_state_version", getattr(plan, "expected_state_version")
    )
    expected_cursor = getattr(plan, "expected_cursor")
    _require_non_negative("expected_cursor", expected_cursor)
    if getattr(plan, "input_sequence_no") != expected_cursor + 1:
        raise ValueError("input_sequence_no must immediately follow expected_cursor")
    _require_optional_non_negative(
        "expected_binding_version", getattr(plan, "expected_binding_version")
    )
    if not isinstance(
        getattr(plan, "operation_terminal_state"), ControlOperationStatus
    ):
        raise TypeError("operation_terminal_state must be a ControlOperationStatus")


def _freeze_typed_tuple(
    owner: object,
    name: str,
    values: object,
    expected_type: type[object] | tuple[type[object], ...],
) -> tuple[object, ...]:
    if not isinstance(values, (tuple, list)):
        raise TypeError(f"{name} must be a tuple or list")
    frozen = tuple(values)
    if any(not isinstance(value, expected_type) for value in frozen):
        raise TypeError(f"{name} contains an invalid mutation type")
    object.__setattr__(owner, name, frozen)
    return frozen


def _freeze_result_events(owner: object, values: object) -> tuple[GameEvent, ...]:
    if not isinstance(values, (tuple, list)):
        raise TypeError("result_events must be a tuple or list")
    frozen = tuple(values)
    if not frozen:
        raise ValueError("result_events must not be empty")
    if any(not isinstance(event, GameEvent) for event in frozen):
        raise TypeError("result_events must contain GameEvent values")
    if len({event.event_id for event in frozen}) != len(frozen):
        raise ValueError("result_event IDs must be unique")
    object.__setattr__(owner, "result_events", frozen)
    return frozen


def _validate_result_events(
    plan: ControlApplyPlan,
    events: tuple[GameEvent, ...],
    result_state_version: int,
) -> None:
    for event in events:
        _validate_one_result_event(plan, event, result_state_version)


def _validate_setup_participant_mutations(plan: ControlApplyPlan) -> None:
    evidence = plan.setup_participant_evidence
    if plan.command_type is SessionCommandType.SET_SCRIPT:
        if len(plan.setup_mutations) != 1 or plan.participant_mutations:
            raise ControlSetupParticipantEvidenceError(
                "SET_SCRIPT requires exactly one Setup mutation"
            )
        mutation = plan.setup_mutations[0]
        script = evidence.script_apply
        if script is None or (
            mutation.mutation_type is not SetupMutationType.SET_SCRIPT
            or mutation.script_id != script.script_id
            or mutation.manifest_reference != script.manifest_reference
            or mutation.manifest_version != script.manifest_version
            or mutation.expected_setup_version != script.expected_setup_version
            or mutation.resulting_setup_version != script.resulting_setup_version
        ):
            raise ControlSetupParticipantEvidenceError(
                "SET_SCRIPT mutation does not match evidence"
            )
    elif plan.command_type is SessionCommandType.ASSIGN_CHARACTER:
        if len(plan.participant_mutations) != 1 or plan.setup_mutations:
            raise ControlSetupParticipantEvidenceError(
                "ASSIGN_CHARACTER requires exactly one Participant mutation"
            )
        mutation = plan.participant_mutations[0]
        assignment = evidence.character_assignment
        if assignment is None or (
            mutation.mutation_type is not ParticipantMutationType.ASSIGN_CHARACTER
            or mutation.participant_id != assignment.participant_id
            or mutation.character_id != assignment.character_id
            or mutation.character_binding_reference
            != assignment.character_binding_reference
            or mutation.expected_binding_version
            != assignment.expected_binding_version
            or mutation.resulting_binding_version
            != assignment.resulting_binding_version
        ):
            raise ControlSetupParticipantEvidenceError(
                "ASSIGN_CHARACTER mutation does not match evidence"
            )
    elif plan.command_type is SessionCommandType.REPLACE_PLAYER:
        if len(plan.participant_mutations) != 1 or plan.setup_mutations:
            raise ControlSetupParticipantEvidenceError(
                "REPLACE_PLAYER requires exactly one Participant mutation"
            )
        mutation = plan.participant_mutations[0]
        replacement = evidence.player_replacement
        if replacement is None or (
            mutation.mutation_type is not ParticipantMutationType.REPLACE
            or mutation.participant_id != replacement.old_participant_id
            or mutation.replacement_participant_id
            != replacement.new_participant_id
            or mutation.old_expected_binding_version
            != replacement.old_expected_binding_version
            or mutation.old_resulting_binding_version
            != replacement.old_resulting_binding_version
            or mutation.new_expected_binding_version
            != replacement.new_expected_binding_version
            or mutation.new_resulting_binding_version
            != replacement.new_resulting_binding_version
            or mutation.old_character_binding_reference
            != replacement.character_binding_reference
            or mutation.new_character_binding_reference
            != replacement.character_binding_reference
        ):
            raise ControlSetupParticipantEvidenceError(
                "REPLACE_PLAYER mutation does not match evidence"
            )
    elif plan.participant_mutations or plan.setup_mutations:
        raise ControlSetupParticipantEvidenceError(
            "command cannot carry Setup / Participant mutations"
        )


def _validate_game_rule_mutations(plan: ControlApplyPlan) -> None:
    if plan.command_type is SessionCommandType.REVEAL_CLUE:
        _validate_clue_reveal_mutation(plan)
        return
    if plan.command_type is not SessionCommandType.ACTIVATE_RULE_SET:
        if plan.game_rule_mutations:
            raise ControlGameRuleEvidenceError(
                "command cannot carry Game Rule mutations"
            )
        return
    if (
        len(plan.game_rule_mutations) != 1
        or plan.participant_mutations
        or plan.setup_mutations
    ):
        raise ControlGameRuleEvidenceError(
            "ACTIVATE_RULE_SET requires exactly one Game Rule mutation"
        )
    mutation = plan.game_rule_mutations[0]
    evidence = plan.game_rule_evidence.rule_set_activation
    candidate = plan.candidate_snapshot
    try:
        validated_candidate = _revalidate_candidate_game_snapshot(candidate)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ControlGameRuleEvidenceError(
            "ACTIVATE_RULE_SET requires a complete CandidateGameSnapshot"
        ) from exc
    setup = validated_candidate.setup
    game_rules = validated_candidate.game_rules
    hidden_state = validated_candidate.hidden_state
    if evidence is None or (
        mutation.mutation_type
        is not GameRuleMutationType.ACTIVATE_RULE_SET
        or mutation.manifest_reference
        != evidence.setup_manifest_reference
        or setup.manifest_reference != evidence.setup_manifest_reference
        or setup.domain_version != evidence.setup_version
        or mutation.committed_rule_set_reference
        != evidence.committed_rule_set_reference
        or game_rules.committed_rule_set_reference
        != evidence.committed_rule_set_reference
        or mutation.committed_disclosure_state_reference
        != evidence.initial_disclosure_state_reference
        or game_rules.committed_disclosure_state_reference
        != evidence.initial_disclosure_state_reference
        or mutation.opaque_hidden_state_reference
        != evidence.opaque_hidden_state_reference
        or hidden_state.committed_state_reference
        != evidence.opaque_hidden_state_reference
        or mutation.expected_game_rule_version
        != evidence.rule_set_version
        or mutation.resulting_game_rule_version
        != game_rules.domain_version
        or mutation.expected_hidden_state_version
        != evidence.hidden_state_version
        or mutation.resulting_hidden_state_version
        != hidden_state.domain_version
        or mutation.provenance_reference
        != evidence.provenance_reference
    ):
        raise ControlGameRuleEvidenceError(
            "ACTIVATE_RULE_SET mutation, evidence, and candidate do not match"
        )


def _validate_clue_reveal_mutation(plan: ControlApplyPlan) -> None:
    if (
        len(plan.game_rule_mutations) != 1
        or plan.participant_mutations
        or plan.setup_mutations
    ):
        raise ControlGameRuleEvidenceError(
            "REVEAL_CLUE requires exactly one clue reveal mutation"
        )
    mutation = plan.game_rule_mutations[0]
    evidence = plan.game_rule_evidence.clue_reveal
    if not isinstance(mutation, ClueRevealMutation) or evidence is None:
        raise ControlGameRuleEvidenceError(
            "REVEAL_CLUE requires bound mutation and evidence"
        )
    try:
        candidate = _revalidate_candidate_game_snapshot(plan.candidate_snapshot)
        event_payload = validate_control_result_event(plan.result_events[0])
    except (AttributeError, IndexError, TypeError, ValueError) as exc:
        raise ControlGameRuleEvidenceError(
            "REVEAL_CLUE requires complete candidate and result Event"
        ) from exc
    game_rules = candidate.game_rules
    hidden_state = candidate.hidden_state
    if (
        mutation.mutation_type is not GameRuleMutationType.REVEAL_CLUE
        or mutation.clue_id != evidence.clue_id
        or mutation.clue_id != getattr(event_payload, "clue_id", None)
        or mutation.active_rule_set_reference
        != evidence.active_rule_set_reference
        or game_rules.committed_rule_set_reference
        != evidence.active_rule_set_reference
        or mutation.public_disclosure_reference
        != evidence.public_disclosure_reference
        or mutation.public_disclosure_reference
        != getattr(event_payload, "public_disclosure_reference", None)
        or mutation.current_disclosure_state_reference
        != evidence.current_disclosure_state_reference
        or mutation.resulting_disclosure_state_reference
        != evidence.resulting_disclosure_state_reference
        or game_rules.committed_disclosure_state_reference
        != evidence.resulting_disclosure_state_reference
        or mutation.current_hidden_state_reference
        != evidence.current_hidden_state_reference
        or mutation.resulting_hidden_state_reference
        != evidence.resulting_hidden_state_reference
        or hidden_state.committed_state_reference
        != evidence.resulting_hidden_state_reference
        or mutation.expected_game_rule_version != evidence.game_rule_version
        or mutation.resulting_game_rule_version != game_rules.domain_version
        or mutation.resulting_game_rule_version
        != getattr(event_payload, "game_rule_domain_version", None)
        or mutation.expected_hidden_state_version != evidence.hidden_state_version
        or mutation.resulting_hidden_state_version != hidden_state.domain_version
        or mutation.provenance_reference != evidence.provenance_reference
    ):
        raise ControlGameRuleEvidenceError(
            "REVEAL_CLUE mutation, evidence, candidate, and Event do not match"
        )


def _revalidate_candidate_game_snapshot(
    candidate: CandidateSessionSnapshot,
) -> object:
    from game_runtime.session_control.composite_snapshot import (
        CandidateGameSnapshot,
        GameRuleSnapshotSlice,
        HiddenGameStateSlice,
        LifecycleSnapshotSlice,
        ParticipantSnapshotRecord,
        ParticipantSnapshotSlice,
        PhaseSnapshotSlice,
        SetupSnapshotSlice,
    )

    if not isinstance(candidate, CandidateGameSnapshot):
        raise TypeError("candidate must be a CandidateGameSnapshot")
    participant_records = candidate.participants.participants
    if not isinstance(participant_records, tuple):
        raise TypeError("candidate participants must be a tuple")
    return CandidateGameSnapshot(
        game_id=candidate.game_id,
        session_id=candidate.session_id,
        group_id=candidate.group_id,
        dm_participant_id=candidate.dm_participant_id,
        status=candidate.status,
        current_phase=candidate.current_phase,
        state_version=candidate.state_version,
        last_applied_sequence_no=candidate.last_applied_sequence_no,
        snapshot_schema_version=candidate.snapshot_schema_version,
        lifecycle=_reconstruct_contract_value(
            candidate.lifecycle,
            LifecycleSnapshotSlice,
        ),
        phase=_reconstruct_contract_value(
            candidate.phase,
            PhaseSnapshotSlice,
        ),
        setup=_reconstruct_contract_value(
            candidate.setup,
            SetupSnapshotSlice,
        ),
        participants=_reconstruct_contract_value(
            candidate.participants,
            ParticipantSnapshotSlice,
            participants=tuple(
                _reconstruct_contract_value(
                    record,
                    ParticipantSnapshotRecord,
                )
                for record in participant_records
            ),
        ),
        game_rules=_reconstruct_contract_value(
            candidate.game_rules,
            GameRuleSnapshotSlice,
        ),
        hidden_state=_reconstruct_contract_value(
            candidate.hidden_state,
            HiddenGameStateSlice,
        ),
    )


def _reconstruct_contract_value(
    value: object,
    expected_type: type,
    **overrides: object,
) -> object:
    if not isinstance(value, expected_type):
        raise TypeError(f"value must be a {expected_type.__name__}")
    arguments = {
        item.name: getattr(value, item.name)
        for item in fields(expected_type)
    }
    arguments.update(overrides)
    return expected_type(**arguments)


def _validate_one_result_event(
    plan: ControlApplyPlan | ControlRejectPlan,
    event: GameEvent,
    result_state_version: int,
) -> None:
    payload = validate_control_result_event(event)
    if event.game_id != plan.game_id or event.session_id != plan.session_id:
        raise ValueError("result Event scope does not match Plan")
    if event.causation_event_id != plan.input_event_id:
        raise ValueError("result Event causation does not match input Event")
    if event.observed_state_version != plan.expected_state_version:
        raise ValueError("result Event observed version does not match Plan")
    if (
        payload.command_id != plan.command_id
        or payload.operation_id != plan.operation_id
        or payload.input_event_id != plan.input_event_id
    ):
        raise ValueError("result Event operation evidence does not match Plan")
    if payload.result_state_version != result_state_version:
        raise ValueError("result Event State version does not match Plan")


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} must not be empty")


def _require_optional_text(name: str, value: object) -> None:
    if value is not None:
        _require_text(name, value)


def _require_non_negative(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must not be negative")


def _require_positive(name: str, value: object) -> None:
    _require_non_negative(name, value)
    if value == 0:
        raise ValueError(f"{name} must be positive")


def _require_optional_non_negative(name: str, value: object) -> None:
    if value is not None:
        _require_non_negative(name, value)


def _require_optional_positive(name: str, value: object) -> None:
    if value is not None:
        _require_positive(name, value)


def _require_time(name: str, value: object) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
