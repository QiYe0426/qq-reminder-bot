"""Pure Setup / Participant ApplyPlan builder for P3-D-6.7."""

from __future__ import annotations

from enum import Enum

from game_runtime.event import (
    CharacterAssignedPayload,
    GameEvent,
    GameEventSource,
    GameEventType,
    PlayerReplacedPayload,
    ScriptSetPayload,
    SessionControlRejectedPayload,
)
from game_runtime.event.control_payloads import (
    CONTROL_RESULT_SCHEMA_VERSION,
    CONTROL_RESULT_VISIBILITY,
    ControlResultPayload,
)
from game_runtime.participant import ParticipantMembershipState, ParticipantType
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control.apply_contract import (
    CandidateSessionSnapshot,
    ControlApplyPlan,
    ControlRejectPlan,
    OwnershipIntent,
    OwnershipIntentType,
    ParticipantMutation,
    ParticipantMutationType,
    SetupMutation,
    SetupMutationType,
)
from game_runtime.session_control.apply_plan_builder import (
    BuildNonCommit,
    BuildNonCommitReason,
    BuildOutcome,
    BuildPlanReady,
    BuildReject,
)
from game_runtime.session_control.build_context import ControlApplyBuildContext
from game_runtime.session_control.commands import (
    AssignCharacterPayload,
    ReplacePlayerPayload,
    SessionCommandType,
    SetScriptPayload,
)
from game_runtime.session_control.operation import ControlOperationStatus
from game_runtime.session_control.setup_participant_evidence import (
    CharacterAvailabilityStatus,
    ControlSetupParticipantEvidenceError,
)


class SetupParticipantRejectReason(str, Enum):
    INVALID_LIFECYCLE = "INVALID_LIFECYCLE"
    SCRIPT_ALREADY_SET = "SCRIPT_ALREADY_SET"
    SCRIPT_REPLACEMENT_NOT_CONFIRMED = "SCRIPT_REPLACEMENT_NOT_CONFIRMED"
    INVALID_CHARACTER_ASSIGNMENT = "INVALID_CHARACTER_ASSIGNMENT"
    CHARACTER_ALREADY_ASSIGNED = "CHARACTER_ALREADY_ASSIGNED"
    CHARACTER_CONFLICT = "CHARACTER_CONFLICT"
    PLAYER_REPLACEMENT_NOT_ALLOWED = "PLAYER_REPLACEMENT_NOT_ALLOWED"
    INVALID_REPLACEMENT = "INVALID_REPLACEMENT"


_SUPPORTED_COMMANDS = frozenset(
    {
        SessionCommandType.SET_SCRIPT,
        SessionCommandType.ASSIGN_CHARACTER,
        SessionCommandType.REPLACE_PLAYER,
    }
)

_SUCCESS_RESULT_CODES = {
    SessionCommandType.SET_SCRIPT: "SET_SCRIPT_APPLIED",
    SessionCommandType.ASSIGN_CHARACTER: "ASSIGN_CHARACTER_APPLIED",
    SessionCommandType.REPLACE_PLAYER: "REPLACE_PLAYER_APPLIED",
}


class SetupParticipantControlApplyPlanBuilder:
    """Deterministically transform immutable evidence without IO or mutation."""

    def build(self, context: ControlApplyBuildContext) -> BuildOutcome:
        if not isinstance(context, ControlApplyBuildContext):
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "CONTROL_APPLY_CONTEXT_TYPE_INVALID",
            )
        if context.command_intent.intent_schema_version != 1:
            return _noncommit(
                BuildNonCommitReason.UNKNOWN_SCHEMA,
                "CONTROL_COMMAND_INTENT_SCHEMA_UNSUPPORTED",
            )
        command_type = context.command_intent.command_type
        if command_type not in _SUPPORTED_COMMANDS:
            return _noncommit(
                BuildNonCommitReason.REDUCER_UNAVAILABLE,
                f"{command_type.value}_REDUCER_UNAVAILABLE",
            )
        if context.session_view.state_version != context.envelope.observed_state_version:
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "CONTROL_STATE_VERSION_MISMATCH",
            )
        try:
            context.setup_participant_evidence.validate_for_command(
                command_type,
                game_id=context.session_view.game_id,
                session_id=context.session_view.session_id,
                observed_state_version=context.session_view.state_version,
            )
        except ControlSetupParticipantEvidenceError:
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                f"{command_type.value}_EVIDENCE_INCOMPLETE",
            )
        reducers = {
            SessionCommandType.SET_SCRIPT: self._build_set_script,
            SessionCommandType.ASSIGN_CHARACTER: self._build_assign_character,
            SessionCommandType.REPLACE_PLAYER: self._build_replace_player,
        }
        return reducers[command_type](context)

    def _build_set_script(self, context: ControlApplyBuildContext) -> BuildOutcome:
        payload = context.command_intent.payload
        evidence = context.setup_participant_evidence.script_apply
        if not isinstance(payload, SetScriptPayload) or evidence is None:
            return _noncommit(BuildNonCommitReason.INVALID_CONTEXT, "SET_SCRIPT_INPUT_INVALID")
        session = context.session_view
        if session.status is GameSessionStatus.PAUSED:
            return _noncommit(
                BuildNonCommitReason.RECOVERY_REQUIRED,
                "SET_SCRIPT_REPAIR_EVIDENCE_REQUIRED",
            )
        if session.status is not GameSessionStatus.CREATED or session.current_phase is not GamePhase.LOBBY:
            return self._reject(context, SetupParticipantRejectReason.INVALID_LIFECYCLE)
        setup = context.setup_view
        if setup is not None:
            if (
                setup.script_id == payload.script_id
                and setup.manifest_reference == payload.manifest_reference
            ):
                return self._reject(context, SetupParticipantRejectReason.SCRIPT_ALREADY_SET)
            if context.envelope.confirmation_reference is None:
                return self._reject(
                    context,
                    SetupParticipantRejectReason.SCRIPT_REPLACEMENT_NOT_CONFIRMED,
                )
        expected_version = 0 if setup is None else setup.setup_version
        if (
            evidence.script_id != payload.script_id
            or evidence.manifest_reference != payload.manifest_reference
            or evidence.expected_setup_version != expected_version
            or evidence.resulting_setup_version != expected_version + 1
        ):
            return _noncommit(BuildNonCommitReason.EVIDENCE_MISMATCH, "SET_SCRIPT_EVIDENCE_MISMATCH")
        candidate = _candidate(context)
        mutation = SetupMutation(
            mutation_type=SetupMutationType.SET_SCRIPT,
            script_id=payload.script_id,
            public_name=payload.public_name,
            manifest_reference=payload.manifest_reference,
            manifest_version=evidence.manifest_version,
            expected_setup_version=evidence.expected_setup_version,
            resulting_setup_version=evidence.resulting_setup_version,
        )
        event = _result_event(
            context,
            event_type=GameEventType.SCRIPT_SET,
            ordinal=1,
            payload=ScriptSetPayload(
                **_common_payload(context, candidate.state_version),
                script_id=payload.script_id,
                public_name=payload.public_name,
                manifest_reference=payload.manifest_reference,
            ),
        )
        return self._ready(context, candidate, (), (mutation,), (event,))

    def _build_assign_character(self, context: ControlApplyBuildContext) -> BuildOutcome:
        payload = context.command_intent.payload
        evidence = context.setup_participant_evidence.character_assignment
        if not isinstance(payload, AssignCharacterPayload) or evidence is None:
            return _noncommit(BuildNonCommitReason.INVALID_CONTEXT, "ASSIGN_CHARACTER_INPUT_INVALID")
        session = context.session_view
        if session.status is GameSessionStatus.PAUSED:
            return _noncommit(BuildNonCommitReason.RECOVERY_REQUIRED, "ASSIGN_CHARACTER_REPAIR_EVIDENCE_REQUIRED")
        if session.status is not GameSessionStatus.CREATED or session.current_phase is not GamePhase.LOBBY:
            return self._reject(context, SetupParticipantRejectReason.INVALID_LIFECYCLE)
        availability = evidence.availability_evidence.availability_status
        if availability is CharacterAvailabilityStatus.UNKNOWN:
            return _noncommit(BuildNonCommitReason.EVIDENCE_MISMATCH, "CHARACTER_AVAILABILITY_UNKNOWN")
        if availability is CharacterAvailabilityStatus.ASSIGNED_TO_TARGET:
            return self._reject(context, SetupParticipantRejectReason.CHARACTER_ALREADY_ASSIGNED)
        if availability is CharacterAvailabilityStatus.ASSIGNED_TO_OTHER:
            return self._reject(context, SetupParticipantRejectReason.CHARACTER_CONFLICT)
        participant = _participant(context, payload.participant_id)
        if (
            participant is None
            or participant.participant_type is not ParticipantType.PLAYER
            or participant.membership_state is not ParticipantMembershipState.ACTIVE
        ):
            return self._reject(context, SetupParticipantRejectReason.INVALID_CHARACTER_ASSIGNMENT)
        if participant.character_id is not None:
            return self._reject(context, SetupParticipantRejectReason.CHARACTER_CONFLICT)
        mutation = ParticipantMutation(
            mutation_type=ParticipantMutationType.ASSIGN_CHARACTER,
            participant_id=payload.participant_id,
            expected_binding_version=evidence.expected_binding_version,
            resulting_binding_version=evidence.resulting_binding_version,
            character_id=payload.character_id,
            character_binding_reference=evidence.character_binding_reference,
        )
        candidate = _candidate(context)
        event = _result_event(
            context,
            event_type=GameEventType.CHARACTER_ASSIGNED,
            ordinal=1,
            payload=CharacterAssignedPayload(
                **_common_payload(context, candidate.state_version),
                participant_id=payload.participant_id,
                character_id=payload.character_id,
                binding_version=evidence.resulting_binding_version,
            ),
        )
        return self._ready(context, candidate, (mutation,), (), (event,))

    def _build_replace_player(self, context: ControlApplyBuildContext) -> BuildOutcome:
        payload = context.command_intent.payload
        evidence = context.setup_participant_evidence.player_replacement
        if not isinstance(payload, ReplacePlayerPayload) or evidence is None:
            return _noncommit(BuildNonCommitReason.INVALID_CONTEXT, "REPLACE_PLAYER_INPUT_INVALID")
        session = context.session_view
        if session.status not in {GameSessionStatus.CREATED, GameSessionStatus.PAUSED}:
            return self._reject(context, SetupParticipantRejectReason.PLAYER_REPLACEMENT_NOT_ALLOWED)
        old = _participant(context, payload.old_participant_id)
        new = _participant(context, payload.new_participant_id)
        if (
            old is None
            or new is None
            or old.participant_type is not ParticipantType.PLAYER
            or new.participant_type is not ParticipantType.PLAYER
            or old.membership_state is not ParticipantMembershipState.ACTIVE
            or new.membership_state is not ParticipantMembershipState.ACTIVE
        ):
            return self._reject(context, SetupParticipantRejectReason.INVALID_REPLACEMENT)
        if new.character_id is not None:
            return self._reject(context, SetupParticipantRejectReason.INVALID_REPLACEMENT)
        binding_reference = evidence.character_binding_reference
        if (old.character_id is None) != (binding_reference is None):
            return _noncommit(BuildNonCommitReason.EVIDENCE_MISMATCH, "REPLACEMENT_CHARACTER_BINDING_MISMATCH")
        mutation = ParticipantMutation(
            mutation_type=ParticipantMutationType.REPLACE,
            participant_id=payload.old_participant_id,
            replacement_participant_id=payload.new_participant_id,
            expected_binding_version=evidence.old_expected_binding_version,
            resulting_binding_version=evidence.new_resulting_binding_version,
            old_expected_binding_version=evidence.old_expected_binding_version,
            old_resulting_binding_version=evidence.old_resulting_binding_version,
            new_expected_binding_version=evidence.new_expected_binding_version,
            new_resulting_binding_version=evidence.new_resulting_binding_version,
            old_character_binding_reference=binding_reference,
            new_character_binding_reference=binding_reference,
        )
        candidate = _candidate(context)
        event = _result_event(
            context,
            event_type=GameEventType.PLAYER_REPLACED,
            ordinal=1,
            payload=PlayerReplacedPayload(
                **_common_payload(context, candidate.state_version),
                old_participant_id=payload.old_participant_id,
                new_participant_id=payload.new_participant_id,
                binding_version=evidence.new_resulting_binding_version,
            ),
        )
        return self._ready(context, candidate, (mutation,), (), (event,))

    def _ready(
        self,
        context: ControlApplyBuildContext,
        candidate: CandidateSessionSnapshot,
        participant_mutations: tuple[ParticipantMutation, ...],
        setup_mutations: tuple[SetupMutation, ...],
        events: tuple[GameEvent, ...],
    ) -> BuildPlanReady:
        session = context.session_view
        envelope = context.envelope
        return BuildPlanReady(
            plan=ControlApplyPlan(
                game_id=session.game_id,
                session_id=session.session_id,
                group_id=session.group_id,
                command_id=envelope.command_id,
                command_type=context.command_intent.command_type,
                operation_id=envelope.operation_id,
                operation_claim_id=context.claim.claim_id,
                input_event_id=envelope.event.event_id,
                input_sequence_no=envelope.event_sequence_no,
                expected_state_version=session.state_version,
                expected_cursor=session.last_applied_sequence_no,
                expected_binding_version=envelope.requester_binding_version,
                candidate_snapshot=candidate,
                participant_mutations=participant_mutations,
                setup_mutations=setup_mutations,
                ownership_intent=_ownership_intent(context),
                result_events=events,
                operation_terminal_state=ControlOperationStatus.SUCCESS,
                setup_participant_evidence=context.setup_participant_evidence,
            )
        )

    def _reject(
        self,
        context: ControlApplyBuildContext,
        reason: SetupParticipantRejectReason,
    ) -> BuildReject:
        session = context.session_view
        envelope = context.envelope
        event = _result_event(
            context,
            event_type=GameEventType.SESSION_CONTROL_REJECTED,
            ordinal=1,
            payload=SessionControlRejectedPayload(
                command_id=envelope.command_id,
                operation_id=envelope.operation_id,
                input_event_id=envelope.event.event_id,
                result_code=reason.value,
                result_state_version=session.state_version,
                reason_code=reason.value,
                state_version=session.state_version,
            ),
        )
        return BuildReject(
            plan=ControlRejectPlan(
                game_id=session.game_id,
                session_id=session.session_id,
                group_id=session.group_id,
                command_id=envelope.command_id,
                command_type=context.command_intent.command_type,
                operation_id=envelope.operation_id,
                operation_claim_id=context.claim.claim_id,
                input_event_id=envelope.event.event_id,
                input_sequence_no=envelope.event_sequence_no,
                expected_state_version=session.state_version,
                expected_cursor=session.last_applied_sequence_no,
                expected_binding_version=envelope.requester_binding_version,
                rejection_event=event,
                operation_terminal_state=ControlOperationStatus.FAILED,
            )
        )


def _participant(context: ControlApplyBuildContext, participant_id: str):
    return next(
        (view for view in context.participant_views if view.participant_id == participant_id),
        None,
    )


def _candidate(context: ControlApplyBuildContext) -> CandidateSessionSnapshot:
    session = context.session_view
    return CandidateSessionSnapshot(
        game_id=session.game_id,
        session_id=session.session_id,
        group_id=session.group_id,
        dm_participant_id=session.dm_participant_id,
        status=session.status,
        current_phase=session.current_phase,
        state_version=session.state_version + 1,
        last_applied_sequence_no=context.envelope.event_sequence_no,
    )


def _ownership_intent(context: ControlApplyBuildContext) -> OwnershipIntent:
    generation = context.ownership_evidence.active_generation
    return OwnershipIntent(
        intent_type=(
            OwnershipIntentType.UNCHANGED
            if generation is None
            else OwnershipIntentType.RETAIN
        ),
        expected_generation=generation,
        resulting_generation=generation,
    )


def _common_payload(
    context: ControlApplyBuildContext, result_state_version: int
) -> dict[str, object]:
    return {
        "command_id": context.envelope.command_id,
        "operation_id": context.envelope.operation_id,
        "input_event_id": context.envelope.event.event_id,
        "result_code": _SUCCESS_RESULT_CODES[context.command_intent.command_type],
        "result_state_version": result_state_version,
    }


def _result_event(
    context: ControlApplyBuildContext,
    *,
    event_type: GameEventType,
    ordinal: int,
    payload: ControlResultPayload,
) -> GameEvent:
    seed = context.result_event_seed
    return GameEvent(
        event_id=seed.derive_event_id(event_type, ordinal=ordinal),
        game_id=seed.game_id,
        session_id=seed.session_id,
        event_type=event_type,
        actor="session-actor",
        source=GameEventSource.CONTROL,
        correlation_id=seed.correlation_id,
        timestamp=seed.timestamp,
        payload=payload.to_mapping(),
        schema_version=CONTROL_RESULT_SCHEMA_VERSION,
        visibility=CONTROL_RESULT_VISIBILITY[event_type],
        observed_state_version=context.session_view.state_version,
        causation_event_id=context.envelope.event.event_id,
    )


def _noncommit(reason: BuildNonCommitReason, detail_code: str) -> BuildNonCommit:
    return BuildNonCommit(reason=reason, detail_code=detail_code)
