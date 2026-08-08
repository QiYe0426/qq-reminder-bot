"""Pure composite-native ApplyPlan builder for participant controls."""

from __future__ import annotations

from dataclasses import fields
from enum import Enum

from game_runtime.event import (
    CharacterAssignedPayload,
    GameEvent,
    GameEventSource,
    GameEventType,
    PlayerReplacedPayload,
    SessionControlRejectedPayload,
)
from game_runtime.event.control_payloads import (
    CONTROL_RESULT_SCHEMA_VERSION,
    CONTROL_RESULT_VISIBILITY,
    ControlResultPayload,
)
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.participant import ParticipantMembershipState, ParticipantType
from game_runtime.session_control.apply_contract import (
    ControlApplyPlan,
    ControlOperationClaim,
    ControlRejectPlan,
    OwnershipIntent,
    OwnershipIntentType,
    ParticipantMutation,
    ParticipantMutationType,
)
from game_runtime.session_control.apply_plan_builder import (
    BuildNonCommit,
    BuildNonCommitReason,
    BuildOutcome,
    BuildPlanReady,
    BuildReject,
)
from game_runtime.session_control.build_context import (
    CanonicalControlCommandIntent,
    ControlApplyBuildContext,
    ControlApplyBuildContextError,
    ControlOwnershipBuildEvidence,
    ControlParticipantBuildView,
    ControlResultEventSeed,
    ControlSessionBuildView,
    ControlSetupBuildView,
)
from game_runtime.session_control.commands import (
    AssignCharacterPayload,
    ReplacePlayerPayload,
    SessionCommandType,
)
from game_runtime.session_control.composite_snapshot import (
    COMPOSITE_SNAPSHOT_SCHEMA_VERSION,
    CandidateGameSnapshot,
    CompositeSnapshotContractError,
    GameRuleSnapshotSlice,
    HiddenGameStateSlice,
    LifecycleSnapshotSlice,
    ParticipantSnapshotRecord,
    ParticipantSnapshotSlice,
    PhaseSnapshotSlice,
    QuestSnapshotSlice,
    SetupSnapshotSlice,
)
from game_runtime.session_control.delivery import ControlEventDeliveryEnvelope
from game_runtime.session_control.lifecycle_evidence import (
    ControlLifecycleApplyEvidence,
    ControlLifecycleEvidenceError,
)
from game_runtime.session_control.operation import ControlOperationStatus
from game_runtime.session_control.participant_transition import (
    AssignCharacterTransitionAccepted,
    AssignCharacterTransitionRejected,
    AssignCharacterTransitionRequest,
    ParticipantTransitionContractError,
    ParticipantTransitionEffect,
    ParticipantTransitionRecordState,
    ReplacePlayerTransitionAccepted,
    ReplacePlayerTransitionRejected,
    ReplacePlayerTransitionRequest,
    transition_participant,
)
from game_runtime.session_control.setup_participant_evidence import (
    CharacterAvailabilityStatus,
    ControlCharacterAssignmentEvidence,
    ControlCharacterAvailabilityEvidence,
    ControlPlayerReplacementEvidence,
    ControlSetupParticipantApplyEvidence,
    ControlSetupParticipantEvidenceError,
)


class ParticipantControlRejectReason(str, Enum):
    INVALID_LIFECYCLE = "INVALID_LIFECYCLE"
    INVALID_CHARACTER_ASSIGNMENT = "INVALID_CHARACTER_ASSIGNMENT"
    CHARACTER_ALREADY_ASSIGNED = "CHARACTER_ALREADY_ASSIGNED"
    CHARACTER_CONFLICT = "CHARACTER_CONFLICT"
    PLAYER_REPLACEMENT_NOT_ALLOWED = "PLAYER_REPLACEMENT_NOT_ALLOWED"
    INVALID_REPLACEMENT = "INVALID_REPLACEMENT"


class ParticipantControlApplyPlanBuilder:
    """Build deterministic participant ApplyPlans from immutable actor evidence."""

    __slots__ = ()

    def build(self, context: ControlApplyBuildContext) -> BuildOutcome:
        if not isinstance(context, ControlApplyBuildContext):
            return _noncommit(BuildNonCommitReason.INVALID_CONTEXT, "CONTROL_APPLY_CONTEXT_TYPE_INVALID")
        intent = getattr(context, "command_intent", None)
        if not isinstance(intent, CanonicalControlCommandIntent):
            return _noncommit(BuildNonCommitReason.INVALID_CONTEXT, "CONTROL_COMMAND_INTENT_INVALID")
        command_type = getattr(intent, "command_type", None)
        if not isinstance(command_type, SessionCommandType):
            return _noncommit(BuildNonCommitReason.INVALID_CONTEXT, "CONTROL_COMMAND_TYPE_INVALID")
        if getattr(intent, "intent_schema_version", None) != 1:
            if not hasattr(intent, "intent_schema_version"):
                return _noncommit(
                    BuildNonCommitReason.INVALID_CONTEXT,
                    "CONTROL_APPLY_CONTEXT_INVALID",
                )
            return _noncommit(BuildNonCommitReason.UNKNOWN_SCHEMA, "CONTROL_COMMAND_INTENT_SCHEMA_UNSUPPORTED")
        if command_type not in {SessionCommandType.ASSIGN_CHARACTER, SessionCommandType.REPLACE_PLAYER}:
            return _noncommit(BuildNonCommitReason.REDUCER_UNAVAILABLE, f"{command_type.value}_REDUCER_UNAVAILABLE")
        if not _revalidate_consumed_context(context):
            return _noncommit(BuildNonCommitReason.INVALID_CONTEXT, "CONTROL_APPLY_CONTEXT_INVALID")
        if context.session_view.state_version != context.envelope.observed_state_version:
            return _noncommit(BuildNonCommitReason.EVIDENCE_MISMATCH, "CONTROL_STATE_VERSION_MISMATCH")
        current = context.session_view.current_game_snapshot
        if current is None:
            return _noncommit(BuildNonCommitReason.EVIDENCE_MISMATCH, "COMPOSITE_CURRENT_SNAPSHOT_MISSING")
        if not isinstance(current, CandidateGameSnapshot):
            return _noncommit(BuildNonCommitReason.INVALID_CONTEXT, "COMPOSITE_CURRENT_SNAPSHOT_INVALID")
        snapshot_schema_version = getattr(current, "snapshot_schema_version", None)
        if snapshot_schema_version is None:
            return _noncommit(BuildNonCommitReason.INVALID_CONTEXT, "COMPOSITE_CURRENT_SNAPSHOT_INVALID")
        if snapshot_schema_version != COMPOSITE_SNAPSHOT_SCHEMA_VERSION:
            return _noncommit(BuildNonCommitReason.UNKNOWN_SCHEMA, "COMPOSITE_SNAPSHOT_SCHEMA_UNSUPPORTED")
        try:
            _revalidate_candidate_snapshot(current)
        except (AttributeError, CompositeSnapshotContractError, TypeError, ValueError):
            return _noncommit(BuildNonCommitReason.INVALID_CONTEXT, "COMPOSITE_CURRENT_SNAPSHOT_INVALID")
        binding_failure = _validate_composite_bindings(context, current)
        if binding_failure is not None:
            return binding_failure
        if (
            context.session_view.status is GameSessionStatus.CREATED
            and context.session_view.current_phase is not GamePhase.LOBBY
        ):
            return _noncommit(BuildNonCommitReason.INVALID_CONTEXT, "COMPOSITE_LIFECYCLE_INVALID")
        try:
            context.lifecycle_evidence.validate_for_command(
                command_type,
                game_id=context.session_view.game_id,
                session_id=context.session_view.session_id,
                observed_state_version=context.session_view.state_version,
            )
        except (AttributeError, ControlLifecycleEvidenceError, TypeError, ValueError):
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "PARTICIPANT_LIFECYCLE_EVIDENCE_INCOMPLETE",
            )
        if intent.command_type is SessionCommandType.ASSIGN_CHARACTER:
            return self._assign(context, current)
        return self._replace(context, current)

    def _assign(self, context: ControlApplyBuildContext, current: CandidateGameSnapshot) -> BuildOutcome:
        payload = context.command_intent.payload
        if type(payload) is not AssignCharacterPayload:
            return _noncommit(BuildNonCommitReason.INVALID_CONTEXT, "ASSIGN_CHARACTER_INPUT_INVALID")
        evidence = _assignment_evidence(context, payload)
        if isinstance(evidence, BuildNonCommit):
            return evidence
        ownership = _ownership_intent(context, evidence.ownership_generation)
        if isinstance(ownership, BuildNonCommit):
            return ownership
        availability = evidence.availability_evidence.availability_status
        if not _availability_matches_current(context, evidence):
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "CHARACTER_AVAILABILITY_BINDING_MISMATCH",
            )
        if context.session_view.status not in {GameSessionStatus.CREATED, GameSessionStatus.PAUSED}:
            return self._reject(context, ParticipantControlRejectReason.INVALID_LIFECYCLE)
        if availability is CharacterAvailabilityStatus.UNKNOWN:
            return _noncommit(BuildNonCommitReason.EVIDENCE_MISMATCH, "CHARACTER_AVAILABILITY_UNKNOWN")
        if availability is CharacterAvailabilityStatus.ASSIGNED_TO_TARGET:
            return self._reject(context, ParticipantControlRejectReason.CHARACTER_ALREADY_ASSIGNED)
        if availability is CharacterAvailabilityStatus.ASSIGNED_TO_OTHER:
            return self._reject(context, ParticipantControlRejectReason.CHARACTER_CONFLICT)
        participant = _view(context, payload.participant_id)
        if participant is None:
            return self._reject(context, ParticipantControlRejectReason.INVALID_CHARACTER_ASSIGNMENT)
        try:
            decision = transition_participant(AssignCharacterTransitionRequest(_state(participant), payload.character_id))
        except (ParticipantTransitionContractError, TypeError, ValueError):
            return _noncommit(BuildNonCommitReason.INVALID_CONTEXT, "ASSIGN_CHARACTER_TRANSITION_CONTEXT_INVALID")
        if isinstance(decision, AssignCharacterTransitionRejected):
            return self._reject(context, ParticipantControlRejectReason(decision.reason.value))
        if not isinstance(decision, AssignCharacterTransitionAccepted):
            return _noncommit(BuildNonCommitReason.INVALID_CONTEXT, "ASSIGN_CHARACTER_TRANSITION_CONTEXT_INVALID")
        return _compose_participant_apply_plan(context=context, current=current, decision=decision, evidence=evidence, ownership=ownership)

    def _replace(self, context: ControlApplyBuildContext, current: CandidateGameSnapshot) -> BuildOutcome:
        payload = context.command_intent.payload
        if type(payload) is not ReplacePlayerPayload:
            return _noncommit(BuildNonCommitReason.INVALID_CONTEXT, "REPLACE_PLAYER_INPUT_INVALID")
        evidence = _replacement_evidence(context, payload)
        if isinstance(evidence, BuildNonCommit):
            return evidence
        ownership = _ownership_intent(context, evidence.ownership_generation)
        if isinstance(ownership, BuildNonCommit):
            return ownership
        old, new = _view(context, payload.old_participant_id), _view(context, payload.new_participant_id)
        if old is None or new is None:
            return self._reject(context, ParticipantControlRejectReason.INVALID_REPLACEMENT)
        binding_failure = _replacement_character_binding_failure(
            context,
            current,
            evidence,
        )
        if binding_failure is not None:
            return binding_failure
        if context.session_view.status not in {GameSessionStatus.CREATED, GameSessionStatus.PAUSED}:
            return self._reject(context, ParticipantControlRejectReason.PLAYER_REPLACEMENT_NOT_ALLOWED)
        try:
            decision = transition_participant(ReplacePlayerTransitionRequest(_state(old), _state(new)))
        except (ParticipantTransitionContractError, TypeError, ValueError):
            return _noncommit(BuildNonCommitReason.INVALID_CONTEXT, "REPLACE_PLAYER_TRANSITION_CONTEXT_INVALID")
        if isinstance(decision, ReplacePlayerTransitionRejected):
            return self._reject(context, ParticipantControlRejectReason(decision.reason.value))
        if not isinstance(decision, ReplacePlayerTransitionAccepted):
            return _noncommit(BuildNonCommitReason.INVALID_CONTEXT, "REPLACE_PLAYER_TRANSITION_CONTEXT_INVALID")
        return _compose_participant_apply_plan(context=context, current=current, decision=decision, evidence=evidence, ownership=ownership)

    def _reject(self, context: ControlApplyBuildContext, reason: ParticipantControlRejectReason) -> BuildReject:
        session, envelope = context.session_view, context.envelope
        event = _result_event(context, GameEventType.SESSION_CONTROL_REJECTED, 1, SessionControlRejectedPayload(
            command_id=envelope.command_id, operation_id=envelope.operation_id,
            input_event_id=envelope.event.event_id, result_code=reason.value,
            result_state_version=session.state_version, reason_code=reason.value,
            state_version=session.state_version,
        ))
        return BuildReject(plan=ControlRejectPlan(
            game_id=session.game_id, session_id=session.session_id, group_id=session.group_id,
            command_id=envelope.command_id, command_type=context.command_intent.command_type,
            operation_id=envelope.operation_id, operation_claim_id=context.claim.claim_id,
            input_event_id=envelope.event.event_id, input_sequence_no=envelope.event_sequence_no,
            expected_state_version=session.state_version, expected_cursor=session.last_applied_sequence_no,
            expected_binding_version=envelope.requester_binding_version, rejection_event=event,
            operation_terminal_state=ControlOperationStatus.FAILED,
        ))


def _compose_participant_apply_plan(*, context: ControlApplyBuildContext, current: CandidateGameSnapshot, decision: AssignCharacterTransitionAccepted | ReplacePlayerTransitionAccepted, evidence: ControlCharacterAssignmentEvidence | ControlPlayerReplacementEvidence, ownership: OwnershipIntent) -> BuildPlanReady | BuildNonCommit:
    """Compose the complete atomic participant plan, or reject incomplete inputs."""
    if not isinstance(context, ControlApplyBuildContext):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "PARTICIPANT_COMPOSITION_INVALID",
        )
    if not _revalidate_consumed_context(context):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "PARTICIPANT_COMPOSITION_INVALID",
        )
    if current is not context.session_view.current_game_snapshot:
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "PARTICIPANT_COMPOSITION_INVALID",
        )
    session, envelope = context.session_view, context.envelope
    try:
        if isinstance(evidence, ControlCharacterAssignmentEvidence):
            evidence = _reconstruct(
                evidence,
                ControlCharacterAssignmentEvidence,
                availability_evidence=_reconstruct(
                    evidence.availability_evidence,
                    ControlCharacterAvailabilityEvidence,
                ),
            )
        elif isinstance(evidence, ControlPlayerReplacementEvidence):
            evidence = _reconstruct(evidence, ControlPlayerReplacementEvidence)
        else:
            raise TypeError("participant command evidence has an invalid type")
        if isinstance(evidence, ControlPlayerReplacementEvidence):
            binding_failure = _replacement_character_binding_failure(
                context,
                current,
                evidence,
            )
            if binding_failure is not None:
                return binding_failure
        if (
            not isinstance(current, CandidateGameSnapshot)
            or _revalidate_candidate_snapshot(current) is None
            or _validate_composite_bindings(context, current) is not None
            or not _composer_ownership_matches(
                context=context,
                evidence=evidence,
                ownership=ownership,
            )
            or not _decision_matches_composition(
                context=context,
                current=current,
                decision=decision,
                evidence=evidence,
            )
        ):
            raise ValueError("participant composition bindings do not match")
        updates: dict[str, ParticipantSnapshotRecord] = {}
        if isinstance(decision, AssignCharacterTransitionAccepted) and isinstance(evidence, ControlCharacterAssignmentEvidence):
            result = decision.resulting_participant
            updates[result.participant_id] = ParticipantSnapshotRecord(result.participant_id, result.participant_type, result.membership_state, result.character_id, result.binding_version)
            mutation = ParticipantMutation(ParticipantMutationType.ASSIGN_CHARACTER, evidence.participant_id, evidence.expected_binding_version, evidence.resulting_binding_version, character_id=evidence.character_id, character_binding_reference=evidence.character_binding_reference)
            event_type, result_code = GameEventType.CHARACTER_ASSIGNED, "ASSIGN_CHARACTER_APPLIED"
            event_payload: ControlResultPayload = CharacterAssignedPayload(command_id=envelope.command_id, operation_id=envelope.operation_id, input_event_id=envelope.event.event_id, result_code=result_code, result_state_version=current.state_version + 1, participant_id=evidence.participant_id, character_id=evidence.character_id, binding_version=evidence.resulting_binding_version)
        elif isinstance(decision, ReplacePlayerTransitionAccepted) and isinstance(evidence, ControlPlayerReplacementEvidence):
            for result in (decision.resulting_old_participant, decision.resulting_new_participant):
                updates[result.participant_id] = ParticipantSnapshotRecord(result.participant_id, result.participant_type, result.membership_state, result.character_id, result.binding_version)
            mutation = ParticipantMutation(ParticipantMutationType.REPLACE, evidence.old_participant_id, evidence.old_expected_binding_version, evidence.new_resulting_binding_version, replacement_participant_id=evidence.new_participant_id, old_expected_binding_version=evidence.old_expected_binding_version, old_resulting_binding_version=evidence.old_resulting_binding_version, new_expected_binding_version=evidence.new_expected_binding_version, new_resulting_binding_version=evidence.new_resulting_binding_version, old_character_binding_reference=evidence.character_binding_reference, new_character_binding_reference=evidence.character_binding_reference)
            event_type, result_code = GameEventType.PLAYER_REPLACED, "REPLACE_PLAYER_APPLIED"
            event_payload = PlayerReplacedPayload(command_id=envelope.command_id, operation_id=envelope.operation_id, input_event_id=envelope.event.event_id, result_code=result_code, result_state_version=current.state_version + 1, old_participant_id=evidence.old_participant_id, new_participant_id=evidence.new_participant_id, binding_version=evidence.new_resulting_binding_version)
        else:
            raise ValueError("decision and evidence must match")
        records = tuple(updates.get(record.participant_id, record) for record in current.participants.participants)
        participants = ParticipantSnapshotSlice(current.participants.schema_version, current.participants.domain_version + 1, tuple(sorted(records, key=lambda record: record.participant_id)))
        candidate = CandidateGameSnapshot(
            game_id=current.game_id, session_id=current.session_id, group_id=current.group_id,
            dm_participant_id=current.dm_participant_id, status=current.status,
            current_phase=current.current_phase, state_version=current.state_version + 1,
            last_applied_sequence_no=envelope.event_sequence_no, snapshot_schema_version=current.snapshot_schema_version,
            lifecycle=current.lifecycle, phase=current.phase, setup=current.setup, participants=participants,
            game_rules=current.game_rules, quest=current.quest,
            hidden_state=current.hidden_state,
        )
        event = _result_event(context, event_type, 1, event_payload)
        plan = ControlApplyPlan(
            game_id=session.game_id, session_id=session.session_id, group_id=session.group_id,
            command_id=envelope.command_id, command_type=context.command_intent.command_type,
            operation_id=envelope.operation_id, operation_claim_id=context.claim.claim_id,
            input_event_id=envelope.event.event_id, input_sequence_no=envelope.event_sequence_no,
            expected_state_version=session.state_version, expected_cursor=session.last_applied_sequence_no,
            expected_binding_version=envelope.requester_binding_version, candidate_snapshot=candidate,
            participant_mutations=(mutation,), setup_mutations=(), ownership_intent=ownership,
            result_events=(event,), operation_terminal_state=ControlOperationStatus.SUCCESS,
            lifecycle_evidence=context.lifecycle_evidence,
            setup_participant_evidence=ControlSetupParticipantApplyEvidence(
                character_assignment=(
                    evidence
                    if isinstance(evidence, ControlCharacterAssignmentEvidence)
                    else None
                ),
                player_replacement=(
                    evidence
                    if isinstance(evidence, ControlPlayerReplacementEvidence)
                    else None
                ),
            ),
        )
    except (AttributeError, CompositeSnapshotContractError, ControlApplyBuildContextError, ControlSetupParticipantEvidenceError, TypeError, ValueError):
        return _noncommit(BuildNonCommitReason.INVALID_CONTEXT, "PARTICIPANT_COMPOSITION_INVALID")
    if (plan.candidate_snapshot is not candidate or plan.participant_mutations != (mutation,) or plan.setup_mutations or plan.result_events != (event,)):
        return _noncommit(BuildNonCommitReason.INVALID_CONTEXT, "PARTICIPANT_COMPOSITION_INVALID")
    return BuildPlanReady(plan=plan)


def _composer_ownership_matches(
    *,
    context: ControlApplyBuildContext,
    evidence: ControlCharacterAssignmentEvidence | ControlPlayerReplacementEvidence,
    ownership: OwnershipIntent,
) -> bool:
    session = context.session_view
    try:
        validated = _reconstruct(
            context.ownership_evidence,
            ControlOwnershipBuildEvidence,
        )
    except (AttributeError, ControlApplyBuildContextError, TypeError, ValueError):
        return False
    if (
        validated.game_id,
        validated.session_id,
        validated.group_id,
        validated.observed_state_version,
    ) != (
        session.game_id,
        session.session_id,
        session.group_id,
        session.state_version,
    ):
        return False
    if (
        context.command_intent.command_type is SessionCommandType.ASSIGN_CHARACTER
        and context.setup_participant_evidence.character_assignment != evidence
    ) or (
        context.command_intent.command_type is SessionCommandType.REPLACE_PLAYER
        and context.setup_participant_evidence.player_replacement != evidence
    ):
        return False
    generation = validated.active_generation
    if evidence.ownership_generation != generation:
        return False
    if session.status is GameSessionStatus.CREATED:
        return (
            ownership.intent_type is OwnershipIntentType.UNCHANGED
            and ownership.expected_generation is None
            and ownership.resulting_generation is None
            and generation is None
            and evidence.ownership_generation is None
        )
    if session.status is GameSessionStatus.PAUSED:
        return (
            ownership.intent_type is OwnershipIntentType.RETAIN
            and isinstance(generation, int)
            and generation > 0
            and ownership.expected_generation == generation
            and ownership.resulting_generation == generation
        )
    return False


def _decision_matches_composition(
    *,
    context: ControlApplyBuildContext,
    current: CandidateGameSnapshot,
    decision: AssignCharacterTransitionAccepted | ReplacePlayerTransitionAccepted,
    evidence: ControlCharacterAssignmentEvidence | ControlPlayerReplacementEvidence,
) -> bool:
    """Require a typed transition result to be exactly bound to current evidence."""

    records = {record.participant_id: record for record in current.participants.participants}
    payload = context.command_intent.payload
    if isinstance(decision, AssignCharacterTransitionAccepted) and isinstance(
        evidence, ControlCharacterAssignmentEvidence
    ):
        previous, resulting = decision.previous_participant, decision.resulting_participant
        record = records.get(evidence.participant_id)
        return (
            type(payload) is AssignCharacterPayload
            and record is not None
            and decision.effect is ParticipantTransitionEffect.ASSIGN_CHARACTER
            and previous == _snapshot_state(record)
            and previous.participant_id == payload.participant_id == evidence.participant_id
            and previous.participant_type is ParticipantType.PLAYER
            and previous.membership_state is ParticipantMembershipState.ACTIVE
            and previous.character_id is None
            and previous.binding_version == evidence.expected_binding_version
            and resulting.participant_id == previous.participant_id
            and resulting.participant_type is ParticipantType.PLAYER
            and resulting.membership_state is ParticipantMembershipState.ACTIVE
            and resulting.character_id == payload.character_id == evidence.character_id
            and resulting.binding_version == evidence.resulting_binding_version
            and resulting.binding_version == previous.binding_version + 1
            and evidence.availability_evidence.availability_status
            is CharacterAvailabilityStatus.AVAILABLE
            and evidence.availability_evidence.occupying_participant_reference
            is None
            and _availability_matches_current(context, evidence)
        )
    if isinstance(decision, ReplacePlayerTransitionAccepted) and isinstance(
        evidence, ControlPlayerReplacementEvidence
    ):
        old_record = records.get(evidence.old_participant_id)
        new_record = records.get(evidence.new_participant_id)
        old_before, new_before = (
            decision.previous_old_participant,
            decision.previous_new_participant,
        )
        old_after, new_after = (
            decision.resulting_old_participant,
            decision.resulting_new_participant,
        )
        return (
            type(payload) is ReplacePlayerPayload
            and old_record is not None
            and new_record is not None
            and decision.effect is ParticipantTransitionEffect.REPLACE_PLAYER
            and old_before == _snapshot_state(old_record)
            and new_before == _snapshot_state(new_record)
            and old_before.participant_id == payload.old_participant_id == evidence.old_participant_id
            and new_before.participant_id == payload.new_participant_id == evidence.new_participant_id
            and old_before.participant_type is ParticipantType.PLAYER
            and new_before.participant_type is ParticipantType.PLAYER
            and old_before.membership_state is ParticipantMembershipState.ACTIVE
            and new_before.membership_state is ParticipantMembershipState.ACTIVE
            and old_before.binding_version == evidence.old_expected_binding_version
            and new_before.binding_version == evidence.new_expected_binding_version
            and old_after.participant_id == old_before.participant_id
            and new_after.participant_id == new_before.participant_id
            and old_after.participant_type is ParticipantType.PLAYER
            and new_after.participant_type is ParticipantType.PLAYER
            and old_after.membership_state is ParticipantMembershipState.REPLACED
            and new_after.membership_state is ParticipantMembershipState.ACTIVE
            and old_after.binding_version == evidence.old_resulting_binding_version
            and new_after.binding_version == evidence.new_resulting_binding_version
            and old_after.binding_version == old_before.binding_version + 1
            and new_after.binding_version == new_before.binding_version + 1
            and old_after.character_id is None
            and new_after.character_id == old_before.character_id
            and (evidence.character_binding_reference is None)
            == (old_before.character_id is None)
        )
    return False


def _assignment_evidence(context: ControlApplyBuildContext, payload: AssignCharacterPayload) -> ControlCharacterAssignmentEvidence | BuildNonCommit:
    try:
        context.setup_participant_evidence.validate_for_command(SessionCommandType.ASSIGN_CHARACTER, game_id=context.session_view.game_id, session_id=context.session_view.session_id, observed_state_version=context.session_view.state_version)
        context.setup_participant_evidence.validate_command_bindings(SessionCommandType.ASSIGN_CHARACTER, payload, authorization_reference=context.envelope.authorization_reference, confirmation_reference=context.envelope.confirmation_reference, active_ownership_generation=context.ownership_evidence.active_generation)
        evidence = context.setup_participant_evidence.character_assignment
        availability = evidence.availability_evidence if evidence is not None else None
        evidence = _reconstruct(evidence, ControlCharacterAssignmentEvidence, availability_evidence=_reconstruct(availability, ControlCharacterAvailabilityEvidence))
    except (AttributeError, ControlSetupParticipantEvidenceError, TypeError, ValueError):
        return _noncommit(BuildNonCommitReason.EVIDENCE_MISMATCH, "ASSIGN_CHARACTER_EVIDENCE_INCOMPLETE")
    if (evidence.participant_id != payload.participant_id or evidence.character_id != payload.character_id or _view(context, payload.participant_id) is None or evidence.expected_binding_version != _view(context, payload.participant_id).binding_version):
        return _noncommit(BuildNonCommitReason.EVIDENCE_MISMATCH, "ASSIGN_CHARACTER_EVIDENCE_MISMATCH")
    return evidence


def _replacement_evidence(context: ControlApplyBuildContext, payload: ReplacePlayerPayload) -> ControlPlayerReplacementEvidence | BuildNonCommit:
    try:
        context.setup_participant_evidence.validate_for_command(SessionCommandType.REPLACE_PLAYER, game_id=context.session_view.game_id, session_id=context.session_view.session_id, observed_state_version=context.session_view.state_version)
        context.setup_participant_evidence.validate_command_bindings(SessionCommandType.REPLACE_PLAYER, payload, authorization_reference=context.envelope.authorization_reference, confirmation_reference=context.envelope.confirmation_reference, active_ownership_generation=context.ownership_evidence.active_generation)
        evidence = _reconstruct(context.setup_participant_evidence.player_replacement, ControlPlayerReplacementEvidence)
    except (AttributeError, ControlSetupParticipantEvidenceError, TypeError, ValueError):
        return _noncommit(BuildNonCommitReason.EVIDENCE_MISMATCH, "REPLACE_PLAYER_EVIDENCE_INCOMPLETE")
    old, new = _view(context, payload.old_participant_id), _view(context, payload.new_participant_id)
    if (old is None or new is None or evidence.old_expected_binding_version != old.binding_version or evidence.new_expected_binding_version != new.binding_version):
        return _noncommit(BuildNonCommitReason.EVIDENCE_MISMATCH, "REPLACE_PLAYER_EVIDENCE_MISMATCH")
    return evidence


def _ownership_intent(context: ControlApplyBuildContext, evidence_generation: int | None) -> OwnershipIntent | BuildNonCommit:
    try:
        ownership = _reconstruct(context.ownership_evidence, ControlOwnershipBuildEvidence)
    except (AttributeError, ControlApplyBuildContextError, TypeError, ValueError):
        return _noncommit(BuildNonCommitReason.EVIDENCE_MISMATCH, "PARTICIPANT_OWNERSHIP_INVALID")
    session = context.session_view
    if (ownership.game_id, ownership.session_id, ownership.group_id, ownership.observed_state_version) != (session.game_id, session.session_id, session.group_id, session.state_version) or evidence_generation != ownership.active_generation:
        return _noncommit(BuildNonCommitReason.EVIDENCE_MISMATCH, "PARTICIPANT_OWNERSHIP_BINDING_MISMATCH")
    if session.status is GameSessionStatus.CREATED:
        if ownership.active_generation is not None or evidence_generation is not None:
            return _noncommit(BuildNonCommitReason.EVIDENCE_MISMATCH, "PARTICIPANT_OWNERSHIP_INVALID")
        return OwnershipIntent(OwnershipIntentType.UNCHANGED, None, None)
    if session.status is GameSessionStatus.PAUSED and ownership.active_generation is not None:
        return OwnershipIntent(OwnershipIntentType.RETAIN, ownership.active_generation, ownership.active_generation)
    if session.status in {GameSessionStatus.RUNNING, GameSessionStatus.ENDED} and ownership.active_generation is not None:
        # This intent is validation-only: these lifecycles reject before plan composition.
        return OwnershipIntent(OwnershipIntentType.RETAIN, ownership.active_generation, ownership.active_generation)
    return _noncommit(BuildNonCommitReason.EVIDENCE_MISMATCH, "PARTICIPANT_OWNERSHIP_INVALID")


def _validate_composite_bindings(context: ControlApplyBuildContext, current: CandidateGameSnapshot) -> BuildNonCommit | None:
    session, event = context.session_view, context.envelope.event
    if (current.game_id, current.session_id, current.group_id, current.dm_participant_id) != (session.game_id, session.session_id, session.group_id, session.dm_participant_id) or (event.game_id, event.session_id) != (session.game_id, session.session_id):
        return _noncommit(BuildNonCommitReason.EVIDENCE_MISMATCH, "COMPOSITE_SCOPE_BINDING_MISMATCH")
    if (current.status is not session.status or current.current_phase is not session.current_phase or current.lifecycle.status is not session.status or current.phase.phase is not session.current_phase):
        return _noncommit(BuildNonCommitReason.EVIDENCE_MISMATCH, "COMPOSITE_LIFECYCLE_BINDING_MISMATCH")
    if current.state_version != session.state_version:
        return _noncommit(BuildNonCommitReason.EVIDENCE_MISMATCH, "COMPOSITE_STATE_VERSION_MISMATCH")
    if current.last_applied_sequence_no > session.last_applied_sequence_no or context.envelope.event_sequence_no != session.last_applied_sequence_no + 1:
        return _noncommit(BuildNonCommitReason.EVIDENCE_MISMATCH, "COMPOSITE_CURSOR_BINDING_MISMATCH")
    try:
        expected = tuple(ParticipantSnapshotRecord(view.participant_id, view.participant_type, view.membership_state, view.character_id, view.binding_version) for view in sorted(context.participant_views, key=lambda view: view.participant_id))
    except (AttributeError, CompositeSnapshotContractError, TypeError, ValueError):
        return _noncommit(BuildNonCommitReason.EVIDENCE_MISMATCH, "COMPOSITE_PARTICIPANT_BINDING_MISMATCH")
    if current.participants.participants != expected:
        return _noncommit(BuildNonCommitReason.EVIDENCE_MISMATCH, "COMPOSITE_PARTICIPANT_BINDING_MISMATCH")
    setup, view = current.setup, context.setup_view
    matches = (view is None and setup.domain_version == 0 and setup.script_id is None and setup.public_name is None and setup.manifest_reference is None and setup.manifest_version is None) if setup.domain_version == 0 else (isinstance(view, ControlSetupBuildView) and (view.game_id, view.session_id, view.script_id, view.public_name, view.manifest_reference, view.setup_version) == (current.game_id, current.session_id, setup.script_id, setup.public_name, setup.manifest_reference, setup.domain_version) and setup.manifest_version is not None)
    if not matches:
        return _noncommit(BuildNonCommitReason.EVIDENCE_MISMATCH, "COMPOSITE_SETUP_BINDING_MISMATCH")
    return None


def _revalidate_consumed_context(context: ControlApplyBuildContext) -> bool:
    try:
        if not all((isinstance(context.envelope, ControlEventDeliveryEnvelope), isinstance(context.claim, ControlOperationClaim), isinstance(context.command_intent, CanonicalControlCommandIntent), isinstance(context.session_view, ControlSessionBuildView), isinstance(context.ownership_evidence, ControlOwnershipBuildEvidence), isinstance(context.result_event_seed, ControlResultEventSeed), isinstance(context.lifecycle_evidence, ControlLifecycleApplyEvidence), isinstance(context.setup_participant_evidence, ControlSetupParticipantApplyEvidence))):
            return False
        event = _reconstruct(context.envelope.event, GameEvent)
        envelope = _reconstruct(context.envelope, ControlEventDeliveryEnvelope, event=event)
        claim = _reconstruct(context.claim, ControlOperationClaim)
        payload = context.command_intent.payload
        if context.command_intent.command_type is SessionCommandType.ASSIGN_CHARACTER:
            payload = _reconstruct(payload, AssignCharacterPayload)
        elif context.command_intent.command_type is SessionCommandType.REPLACE_PLAYER:
            payload = _reconstruct(payload, ReplacePlayerPayload)
        intent = _reconstruct(context.command_intent, CanonicalControlCommandIntent, payload=payload)
        session = _reconstruct(context.session_view, ControlSessionBuildView, current_game_snapshot=context.session_view.current_game_snapshot)
        participants = tuple(_reconstruct(view, ControlParticipantBuildView) for view in context.participant_views)
        setup = None if context.setup_view is None else _reconstruct(context.setup_view, ControlSetupBuildView)
        ownership = _reconstruct(context.ownership_evidence, ControlOwnershipBuildEvidence)
        seed = _reconstruct(context.result_event_seed, ControlResultEventSeed)
        assignment = context.setup_participant_evidence.character_assignment
        if assignment is not None:
            assignment = _reconstruct(assignment, ControlCharacterAssignmentEvidence, availability_evidence=_reconstruct(assignment.availability_evidence, ControlCharacterAvailabilityEvidence))
        replacement = context.setup_participant_evidence.player_replacement
        if replacement is not None:
            replacement = _reconstruct(replacement, ControlPlayerReplacementEvidence)
        evidence = ControlSetupParticipantApplyEvidence(character_assignment=assignment, player_replacement=replacement, script_apply=context.setup_participant_evidence.script_apply)
        ControlApplyBuildContext(envelope=envelope, claim=claim, command_intent=intent, session_view=session, participant_views=participants, setup_view=setup, ownership_evidence=ownership, result_event_seed=seed, lifecycle_evidence=context.lifecycle_evidence, setup_participant_evidence=evidence)
    except (AttributeError, ControlApplyBuildContextError, ControlSetupParticipantEvidenceError, TypeError, ValueError):
        return False
    return True


def _revalidate_candidate_snapshot(current: CandidateGameSnapshot) -> CandidateGameSnapshot:
    participants = _reconstruct(current.participants, ParticipantSnapshotSlice, participants=tuple(_reconstruct(record, ParticipantSnapshotRecord) for record in current.participants.participants))
    return CandidateGameSnapshot(game_id=current.game_id, session_id=current.session_id, group_id=current.group_id, dm_participant_id=current.dm_participant_id, status=current.status, current_phase=current.current_phase, state_version=current.state_version, last_applied_sequence_no=current.last_applied_sequence_no, snapshot_schema_version=current.snapshot_schema_version, lifecycle=_reconstruct(current.lifecycle, LifecycleSnapshotSlice), phase=_reconstruct(current.phase, PhaseSnapshotSlice), setup=_reconstruct(current.setup, SetupSnapshotSlice), participants=participants, game_rules=_reconstruct(current.game_rules, GameRuleSnapshotSlice), quest=_reconstruct(current.quest, QuestSnapshotSlice), hidden_state=_reconstruct(current.hidden_state, HiddenGameStateSlice))


def _view(context: ControlApplyBuildContext, participant_id: str):
    return next((view for view in context.participant_views if view.participant_id == participant_id), None)


def _availability_matches_current(
    context: ControlApplyBuildContext,
    evidence: ControlCharacterAssignmentEvidence,
) -> bool:
    """Bind the claimed roster availability to the actor-turn participant view."""

    occupants = tuple(
        view.participant_id
        for view in context.participant_views
        if view.character_id == evidence.character_id
    )
    availability = evidence.availability_evidence
    if availability.availability_status is CharacterAvailabilityStatus.UNKNOWN:
        return availability.occupying_participant_reference is None
    if not occupants:
        return (
            availability.availability_status is CharacterAvailabilityStatus.AVAILABLE
            and availability.occupying_participant_reference is None
        )
    if occupants == (evidence.participant_id,):
        return (
            availability.availability_status
            is CharacterAvailabilityStatus.ASSIGNED_TO_TARGET
            and availability.occupying_participant_reference
            == evidence.participant_id
        )
    return len(occupants) == 1 and (
        availability.availability_status
        is CharacterAvailabilityStatus.ASSIGNED_TO_OTHER
        and availability.occupying_participant_reference == occupants[0]
    )


def _replacement_character_binding_failure(
    context: ControlApplyBuildContext,
    current: CandidateGameSnapshot,
    evidence: ControlPlayerReplacementEvidence,
) -> BuildNonCommit | None:
    old = _view(context, evidence.old_participant_id)
    if old is None:
        return None
    if (old.character_id is None) != (
        evidence.character_binding_reference is None
    ):
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "REPLACEMENT_CHARACTER_BINDING_MISMATCH",
        )
    if old.character_id is None:
        return None
    view_occupants = tuple(
        view.participant_id
        for view in context.participant_views
        if view.character_id == old.character_id
    )
    snapshot_occupants = tuple(
        record.participant_id
        for record in current.participants.participants
        if record.character_id == old.character_id
    )
    if view_occupants != (old.participant_id,) or snapshot_occupants != (
        old.participant_id,
    ):
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "REPLACEMENT_CHARACTER_BINDING_MISMATCH",
        )
    return None


def _state(view: ControlParticipantBuildView) -> ParticipantTransitionRecordState:
    return ParticipantTransitionRecordState(view.participant_id, view.participant_type, view.membership_state, view.character_id, view.binding_version)


def _snapshot_state(record: ParticipantSnapshotRecord) -> ParticipantTransitionRecordState:
    return ParticipantTransitionRecordState(
        record.participant_id,
        record.participant_type,
        record.membership_state,
        record.character_id,
        record.binding_version,
    )


def _result_event(context: ControlApplyBuildContext, event_type: GameEventType, ordinal: int, payload: ControlResultPayload) -> GameEvent:
    seed = context.result_event_seed
    return GameEvent(event_id=seed.derive_event_id(event_type, ordinal=ordinal), game_id=seed.game_id, session_id=seed.session_id, event_type=event_type, actor="session-actor", source=GameEventSource.CONTROL, correlation_id=seed.correlation_id, timestamp=seed.timestamp, payload=payload.to_mapping(), schema_version=CONTROL_RESULT_SCHEMA_VERSION, visibility=CONTROL_RESULT_VISIBILITY[event_type], observed_state_version=context.session_view.state_version, causation_event_id=context.envelope.event.event_id)


def _reconstruct(value: object, expected_type: type, **overrides: object) -> object:
    if not isinstance(value, expected_type):
        raise TypeError(f"value must be a {expected_type.__name__}")
    arguments = {field.name: getattr(value, field.name) for field in fields(expected_type)}
    arguments.update(overrides)
    return expected_type(**arguments)


def _noncommit(reason: BuildNonCommitReason, detail_code: str) -> BuildNonCommit:
    return BuildNonCommit(reason=reason, detail_code=detail_code)
