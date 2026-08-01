"""Immutable input evidence for a future Session Control ApplyPlan builder."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json

from game_runtime.event import CONTROL_RESULT_PAYLOAD_TYPES, GameEventType
from game_runtime.participant import ParticipantMembershipState, ParticipantType
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control.apply_contract import ControlOperationClaim
from game_runtime.session_control.commands import (
    ActivateRuleSetPayload,
    AssignCharacterPayload,
    ChangePhasePayload,
    EndGamePayload,
    PauseGamePayload,
    ReplacePlayerPayload,
    ResumeGamePayload,
    SessionCommandPayload,
    SessionCommandType,
    SetScriptPayload,
    StartGamePayload,
)
from game_runtime.session_control.composite_snapshot import CandidateGameSnapshot
from game_runtime.session_control.confirmation import fingerprint_payload
from game_runtime.session_control.delivery import ControlEventDeliveryEnvelope
from game_runtime.session_control.event_integration import validate_dm_command_event
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


class ControlApplyBuildContextError(ValueError):
    """Raised when immutable builder evidence is incomplete or inconsistent."""


_PAYLOAD_TYPE_BY_COMMAND: dict[
    SessionCommandType, type[SessionCommandPayload]
] = {
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
class CanonicalControlCommandIntent:
    """Typed command intent recovered from immutable persisted evidence."""

    intent_schema_version: int
    command_type: SessionCommandType
    payload: SessionCommandPayload
    payload_fingerprint: str

    def __post_init__(self) -> None:
        _require_positive_int("intent_schema_version", self.intent_schema_version)
        if not isinstance(self.command_type, SessionCommandType):
            raise ControlApplyBuildContextError(
                "command_type must be a SessionCommandType"
            )
        expected_payload_type = _PAYLOAD_TYPE_BY_COMMAND.get(self.command_type)
        if expected_payload_type is None:
            raise ControlApplyBuildContextError(
                "CREATE_SESSION must use the bootstrap build path"
            )
        if type(self.payload) is not expected_payload_type:
            raise ControlApplyBuildContextError(
                "canonical payload type does not match command_type"
            )
        _require_sha256("payload_fingerprint", self.payload_fingerprint)
        if fingerprint_payload(self.payload) != self.payload_fingerprint:
            raise ControlApplyBuildContextError(
                "canonical payload does not match payload_fingerprint"
            )


@dataclass(frozen=True, slots=True)
class ControlSessionBuildView:
    """Actor-turn Session facts needed by a future pure reducer."""

    game_id: str
    session_id: str
    group_id: str
    dm_participant_id: str
    status: GameSessionStatus
    current_phase: GamePhase
    state_version: int
    last_applied_sequence_no: int
    current_game_snapshot: CandidateGameSnapshot | None = None
    game_rule_evidence: ControlGameRuleApplyEvidence = field(
        default_factory=ControlGameRuleApplyEvidence
    )

    def __post_init__(self) -> None:
        for name in ("game_id", "session_id", "group_id", "dm_participant_id"):
            _require_text(name, getattr(self, name))
        if not isinstance(self.status, GameSessionStatus):
            raise ControlApplyBuildContextError(
                "status must be a GameSessionStatus"
            )
        if not isinstance(self.current_phase, GamePhase):
            raise ControlApplyBuildContextError("current_phase must be a GamePhase")
        _require_non_negative_int("state_version", self.state_version)
        _require_non_negative_int(
            "last_applied_sequence_no", self.last_applied_sequence_no
        )
        _require_type(
            "game_rule_evidence",
            self.game_rule_evidence,
            ControlGameRuleApplyEvidence,
        )
        try:
            self.game_rule_evidence.validate_bindings(
                game_id=self.game_id,
                session_id=self.session_id,
                observed_state_version=self.state_version,
            )
        except ControlGameRuleEvidenceError as exc:
            raise ControlApplyBuildContextError(str(exc)) from exc
        snapshot = self.current_game_snapshot
        if snapshot is not None:
            _require_type(
                "current_game_snapshot",
                snapshot,
                CandidateGameSnapshot,
            )
            if (
                snapshot.game_id,
                snapshot.session_id,
                snapshot.group_id,
                snapshot.dm_participant_id,
            ) != (
                self.game_id,
                self.session_id,
                self.group_id,
                self.dm_participant_id,
            ):
                raise ControlApplyBuildContextError(
                    "current Game Snapshot scope does not match Session view"
                )
            if (
                snapshot.status is not self.status
                or snapshot.current_phase is not self.current_phase
            ):
                raise ControlApplyBuildContextError(
                    "current Game Snapshot lifecycle does not match Session view"
                )
            if snapshot.state_version != self.state_version:
                raise ControlApplyBuildContextError(
                    "current Game Snapshot version does not match Session view"
                )
            if (
                snapshot.last_applied_sequence_no
                > self.last_applied_sequence_no
            ):
                raise ControlApplyBuildContextError(
                    "current Game Snapshot cursor exceeds Session control cursor"
                )


@dataclass(frozen=True, slots=True)
class ControlParticipantBuildView:
    """Privacy-minimal participant facts captured for one Actor turn."""

    game_id: str
    session_id: str
    participant_id: str
    participant_type: ParticipantType
    membership_state: ParticipantMembershipState
    character_id: str | None
    binding_version: int

    def __post_init__(self) -> None:
        for name in ("game_id", "session_id", "participant_id"):
            _require_text(name, getattr(self, name))
        if not isinstance(self.participant_type, ParticipantType):
            raise ControlApplyBuildContextError(
                "participant_type must be a ParticipantType"
            )
        if not isinstance(self.membership_state, ParticipantMembershipState):
            raise ControlApplyBuildContextError(
                "membership_state must be a ParticipantMembershipState"
            )
        if self.character_id is not None:
            _require_text("character_id", self.character_id)
        _require_non_negative_int("binding_version", self.binding_version)


@dataclass(frozen=True, slots=True)
class ControlSetupBuildView:
    """Immutable setup projection captured before deterministic plan building."""

    game_id: str
    session_id: str
    script_id: str
    public_name: str
    manifest_reference: str
    setup_version: int

    def __post_init__(self) -> None:
        for name in (
            "game_id",
            "session_id",
            "script_id",
            "public_name",
            "manifest_reference",
        ):
            _require_text(name, getattr(self, name))
        _require_non_negative_int("setup_version", self.setup_version)


@dataclass(frozen=True, slots=True)
class ControlOwnershipBuildEvidence:
    """Ownership projection bound to the same Actor-turn state version."""

    game_id: str
    session_id: str
    group_id: str
    active_generation: int | None
    last_allocated_generation: int
    observed_state_version: int

    def __post_init__(self) -> None:
        for name in ("game_id", "session_id", "group_id"):
            _require_text(name, getattr(self, name))
        if self.active_generation is not None:
            _require_positive_int("active_generation", self.active_generation)
        _require_non_negative_int(
            "last_allocated_generation", self.last_allocated_generation
        )
        _require_non_negative_int(
            "observed_state_version", self.observed_state_version
        )
        if (
            self.active_generation is not None
            and self.active_generation > self.last_allocated_generation
        ):
            raise ControlApplyBuildContextError(
                "active_generation cannot exceed last_allocated_generation"
            )


@dataclass(frozen=True, slots=True)
class ControlResultEventSeed:
    """Persisted seed used to derive stable Result Event identities."""

    seed_contract_version: int
    game_id: str
    session_id: str
    command_id: str
    operation_id: str
    input_event_id: str
    timestamp: datetime
    correlation_id: str

    def __post_init__(self) -> None:
        if self.seed_contract_version != 1:
            raise ControlApplyBuildContextError(
                "unsupported result Event seed contract version"
            )
        for name in (
            "game_id",
            "session_id",
            "command_id",
            "operation_id",
            "input_event_id",
            "correlation_id",
        ):
            _require_text(name, getattr(self, name))
        _require_aware_time("timestamp", self.timestamp)

    def derive_event_id(self, event_type: GameEventType, *, ordinal: int) -> str:
        """Derive one deterministic identity without clock, random, or storage."""

        if event_type not in CONTROL_RESULT_PAYLOAD_TYPES:
            raise ControlApplyBuildContextError(
                "event_type must be a controlled Result Event type"
            )
        _require_positive_int("ordinal", ordinal)
        canonical = [
            "CONTROL_RESULT_EVENT_ID_V1",
            self.game_id,
            self.session_id,
            self.command_id,
            self.operation_id,
            self.input_event_id,
            event_type.value,
            ordinal,
        ]
        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"control-result-v1:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class ControlApplyBuildContext:
    """Complete, immutable evidence passed to a pure ApplyPlan builder."""

    envelope: ControlEventDeliveryEnvelope
    claim: ControlOperationClaim
    command_intent: CanonicalControlCommandIntent
    session_view: ControlSessionBuildView
    participant_views: tuple[ControlParticipantBuildView, ...]
    setup_view: ControlSetupBuildView | None
    ownership_evidence: ControlOwnershipBuildEvidence
    result_event_seed: ControlResultEventSeed
    lifecycle_evidence: ControlLifecycleApplyEvidence = field(
        default_factory=ControlLifecycleApplyEvidence
    )
    setup_participant_evidence: ControlSetupParticipantApplyEvidence = field(
        default_factory=ControlSetupParticipantApplyEvidence
    )

    def __post_init__(self) -> None:
        _require_type("envelope", self.envelope, ControlEventDeliveryEnvelope)
        _require_type("claim", self.claim, ControlOperationClaim)
        _require_type(
            "command_intent",
            self.command_intent,
            CanonicalControlCommandIntent,
        )
        _require_type("session_view", self.session_view, ControlSessionBuildView)
        _require_type(
            "ownership_evidence",
            self.ownership_evidence,
            ControlOwnershipBuildEvidence,
        )
        _require_type(
            "result_event_seed", self.result_event_seed, ControlResultEventSeed
        )
        _require_type(
            "lifecycle_evidence",
            self.lifecycle_evidence,
            ControlLifecycleApplyEvidence,
        )
        _require_type(
            "setup_participant_evidence",
            self.setup_participant_evidence,
            ControlSetupParticipantApplyEvidence,
        )
        if self.setup_view is not None:
            _require_type("setup_view", self.setup_view, ControlSetupBuildView)

        if not isinstance(self.participant_views, (tuple, list)):
            raise ControlApplyBuildContextError(
                "participant_views must be a tuple or list"
            )
        participant_views = tuple(self.participant_views)
        if any(
            not isinstance(view, ControlParticipantBuildView)
            for view in participant_views
        ):
            raise ControlApplyBuildContextError(
                "participant_views contains invalid evidence"
            )
        participant_ids = tuple(view.participant_id for view in participant_views)
        if len(participant_ids) != len(set(participant_ids)):
            raise ControlApplyBuildContextError(
                "participant_views must contain unique participant_id values"
            )
        object.__setattr__(self, "participant_views", participant_views)

        payload = validate_dm_command_event(self.envelope.event)
        event = self.envelope.event
        expected_scope = (event.game_id, event.session_id)
        if (self.claim.game_id, self.claim.session_id) != expected_scope:
            raise ControlApplyBuildContextError("claim scope does not match Event")
        if (
            self.claim.command_id != self.envelope.command_id
            or self.claim.operation_id != self.envelope.operation_id
            or self.claim.input_event_id != event.event_id
        ):
            raise ControlApplyBuildContextError(
                "claim identity does not match delivery evidence"
            )
        if (
            self.command_intent.command_type is not payload.command_type
            or self.command_intent.payload_fingerprint
            != payload.payload_fingerprint
        ):
            raise ControlApplyBuildContextError(
                "canonical command intent does not match input Event"
            )

        session = self.session_view
        if (session.game_id, session.session_id) != expected_scope:
            raise ControlApplyBuildContextError(
                "Session build view scope does not match Event"
            )
        if self.envelope.event_sequence_no != session.last_applied_sequence_no + 1:
            raise ControlApplyBuildContextError(
                "input Event sequence must immediately follow the Session cursor"
            )

        for participant in participant_views:
            if (participant.game_id, participant.session_id) != expected_scope:
                raise ControlApplyBuildContextError(
                    "participant build view scope does not match Event"
                )
        requester = next(
            (
                participant
                for participant in participant_views
                if participant.participant_id
                == self.envelope.requester_principal_ref
            ),
            None,
        )
        if requester is None:
            raise ControlApplyBuildContextError(
                "requester participant evidence is missing"
            )
        if requester.binding_version != self.envelope.requester_binding_version:
            raise ControlApplyBuildContextError(
                "requester binding evidence does not match delivery evidence"
            )

        if self.setup_view is not None and (
            self.setup_view.game_id,
            self.setup_view.session_id,
        ) != expected_scope:
            raise ControlApplyBuildContextError(
                "setup build view scope does not match Event"
            )

        ownership = self.ownership_evidence
        if (
            ownership.game_id,
            ownership.session_id,
            ownership.group_id,
        ) != (event.game_id, event.session_id, session.group_id):
            raise ControlApplyBuildContextError(
                "ownership evidence scope does not match Session build view"
            )
        if ownership.observed_state_version != session.state_version:
            raise ControlApplyBuildContextError(
                "ownership evidence state version does not match Session build view"
            )

        seed = self.result_event_seed
        if (
            seed.game_id,
            seed.session_id,
            seed.command_id,
            seed.operation_id,
            seed.input_event_id,
            seed.correlation_id,
        ) != (
            event.game_id,
            event.session_id,
            self.envelope.command_id,
            self.envelope.operation_id,
            event.event_id,
            self.envelope.correlation_id,
        ):
            raise ControlApplyBuildContextError(
                "Result Event seed does not match delivery evidence"
            )
        if seed.timestamp != self.claim.claimed_at:
            raise ControlApplyBuildContextError(
                "Result Event seed timestamp must come from claim evidence"
            )

        try:
            self.lifecycle_evidence.validate_bindings(
                game_id=event.game_id,
                session_id=event.session_id,
                observed_state_version=session.state_version,
            )
        except ControlLifecycleEvidenceError as exc:
            raise ControlApplyBuildContextError(str(exc)) from exc

        try:
            self.setup_participant_evidence.validate_bindings(
                game_id=event.game_id,
                session_id=event.session_id,
                observed_state_version=session.state_version,
            )
            self.setup_participant_evidence.validate_command_bindings(
                self.command_intent.command_type,
                self.command_intent.payload,
                authorization_reference=self.envelope.authorization_reference,
                confirmation_reference=self.envelope.confirmation_reference,
                active_ownership_generation=ownership.active_generation,
            )
            self._validate_setup_participant_projection_bindings()
        except ControlSetupParticipantEvidenceError as exc:
            raise ControlApplyBuildContextError(str(exc)) from exc

    def _validate_setup_participant_projection_bindings(self) -> None:
        evidence = self.setup_participant_evidence
        if evidence.script_apply is not None:
            setup_version = 0 if self.setup_view is None else self.setup_view.setup_version
            if evidence.script_apply.expected_setup_version != setup_version:
                raise ControlSetupParticipantEvidenceError(
                    "SET_SCRIPT evidence setup version does not match Actor turn"
                )

        participants = {
            view.participant_id: view for view in self.participant_views
        }
        if evidence.character_assignment is not None:
            assignment = evidence.character_assignment
            participant = participants.get(assignment.participant_id)
            if (
                participant is None
                or participant.binding_version != assignment.expected_binding_version
            ):
                raise ControlSetupParticipantEvidenceError(
                    "ASSIGN_CHARACTER participant binding does not match Actor turn"
                )

        if evidence.player_replacement is not None:
            replacement = evidence.player_replacement
            old_participant = participants.get(replacement.old_participant_id)
            new_participant = participants.get(replacement.new_participant_id)
            if (
                old_participant is None
                or new_participant is None
                or old_participant.binding_version
                != replacement.old_expected_binding_version
                or new_participant.binding_version
                != replacement.new_expected_binding_version
            ):
                raise ControlSetupParticipantEvidenceError(
                    "REPLACE_PLAYER bilateral binding does not match Actor turn"
                )


def _require_type(name: str, value: object, expected_type: type[object]) -> None:
    if not isinstance(value, expected_type):
        raise ControlApplyBuildContextError(
            f"{name} must be a {expected_type.__name__}"
        )


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ControlApplyBuildContextError(f"{name} must be non-empty text")


def _require_non_negative_int(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ControlApplyBuildContextError(
            f"{name} must be a non-negative integer"
        )


def _require_positive_int(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ControlApplyBuildContextError(f"{name} must be a positive integer")


def _require_sha256(name: str, value: object) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ControlApplyBuildContextError(
            f"{name} must be lowercase SHA-256 hex"
        )


def _require_aware_time(name: str, value: object) -> None:
    if not isinstance(value, datetime):
        raise ControlApplyBuildContextError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ControlApplyBuildContextError(f"{name} must be timezone-aware")
