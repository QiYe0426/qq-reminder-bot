"""Pure ApplyPlan builder for the ACTIVATE_QUEST vertical slice."""

from __future__ import annotations

from dataclasses import fields
from enum import Enum

from game_runtime.event import (
    GameEvent,
    GameEventSource,
    GameEventType,
    QuestActivatedPayload,
    SessionControlRejectedPayload,
)
from game_runtime.event.control_payloads import (
    CONTROL_RESULT_SCHEMA_VERSION,
    CONTROL_RESULT_VISIBILITY,
    ControlResultPayload,
)
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control.apply_contract import (
    ControlApplyPlan,
    ControlRejectPlan,
    OwnershipIntent,
    OwnershipIntentType,
    QuestActivationMutation,
    QuestMutationType,
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
    ControlSessionBuildView,
)
from game_runtime.session_control.commands import (
    ActivateQuestPayload,
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
from game_runtime.session_control.game_rule_evidence import (
    ControlGameRuleEvidenceError,
)
from game_runtime.session_control.lifecycle_evidence import (
    ControlLifecycleEvidenceError,
)
from game_runtime.session_control.operation import ControlOperationStatus
from game_runtime.session_control.quest_evidence import (
    ControlQuestActivationEvidence,
    ControlQuestEvidenceError,
    QuestActivationDisposition,
)
from game_runtime.session_control.quest_transition import (
    QuestActivationAccepted,
    QuestActivationContractError,
    QuestActivationRejected,
    QuestActivationRejectReason,
    QuestActivationRequest,
    QuestActivationState,
    transition_quest_activation,
)
from game_runtime.session_control.setup_participant_evidence import (
    ControlSetupParticipantEvidenceError,
)


class QuestControlRejectReason(str, Enum):
    INVALID_LIFECYCLE = "INVALID_LIFECYCLE"
    INVALID_PHASE = "INVALID_PHASE"
    RULE_SET_NOT_ACTIVE = "RULE_SET_NOT_ACTIVE"
    QUEST_ALREADY_ACTIVE = "QUEST_ALREADY_ACTIVE"
    QUEST_CONFLICT = "QUEST_CONFLICT"
    QUEST_NOT_FOUND = "QUEST_NOT_FOUND"
    QUEST_NOT_ACTIVATABLE = "QUEST_NOT_ACTIVATABLE"


class QuestControlApplyPlanBuilder:
    """Validate one Actor turn and compose one complete Quest plan."""

    __slots__ = ()

    def build(self, context: ControlApplyBuildContext) -> BuildOutcome:
        if not isinstance(context, ControlApplyBuildContext):
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "CONTROL_APPLY_CONTEXT_TYPE_INVALID",
            )
        validated = _validated_input(context)
        if isinstance(validated, BuildNonCommit):
            return validated
        current, evidence, payload = validated
        session = context.session_view
        if session.status is not GameSessionStatus.RUNNING:
            return _reject(context, QuestControlRejectReason.INVALID_LIFECYCLE)
        if session.current_phase not in {
            GamePhase.INTRODUCTION,
            GamePhase.EXPLORATION,
        }:
            return _reject(context, QuestControlRejectReason.INVALID_PHASE)
        try:
            decision = transition_quest_activation(
                QuestActivationRequest(
                    quest_id=payload.quest_id,
                    current_state=QuestActivationState(
                        current.quest.active_quest_id,
                        current.quest.source_rule_set_reference,
                        current.quest.committed_public_state_reference,
                    ),
                    requested_state=(
                        QuestActivationState(
                            evidence.quest_id,
                            evidence.active_rule_set_reference,
                            evidence.resulting_public_state_reference,
                        )
                        if evidence.disposition
                        is QuestActivationDisposition.AVAILABLE
                        else None
                    ),
                    disposition=evidence.disposition,
                )
            )
        except (QuestActivationContractError, TypeError, ValueError):
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "ACTIVATE_QUEST_TRANSITION_INVALID",
            )
        if isinstance(decision, QuestActivationRejected):
            reason = {
                QuestActivationRejectReason.QUEST_ALREADY_ACTIVE:
                    QuestControlRejectReason.QUEST_ALREADY_ACTIVE,
                QuestActivationRejectReason.QUEST_CONFLICT:
                    QuestControlRejectReason.QUEST_CONFLICT,
                QuestActivationRejectReason.QUEST_NOT_FOUND:
                    QuestControlRejectReason.QUEST_NOT_FOUND,
                QuestActivationRejectReason.QUEST_NOT_ACTIVATABLE:
                    QuestControlRejectReason.QUEST_NOT_ACTIVATABLE,
                QuestActivationRejectReason.RULE_SET_NOT_ACTIVE:
                    QuestControlRejectReason.RULE_SET_NOT_ACTIVE,
            }.get(decision.reason)
            if reason is None:
                return _noncommit(
                    BuildNonCommitReason.INVALID_CONTEXT,
                    "ACTIVATE_QUEST_TRANSITION_INVALID",
                )
            return _reject(context, reason)
        if not isinstance(decision, QuestActivationAccepted):
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "ACTIVATE_QUEST_TRANSITION_INVALID",
            )
        return _compose(context, current, evidence, decision)


def _validated_input(
    context: ControlApplyBuildContext,
) -> tuple[
    CandidateGameSnapshot,
    ControlQuestActivationEvidence,
    ActivateQuestPayload,
] | BuildNonCommit:
    try:
        intent = context.command_intent
        if not isinstance(intent, CanonicalControlCommandIntent):
            raise TypeError("invalid command intent")
        if intent.intent_schema_version != 1:
            return _noncommit(
                BuildNonCommitReason.UNKNOWN_SCHEMA,
                "CONTROL_COMMAND_INTENT_SCHEMA_UNSUPPORTED",
            )
        if intent.command_type is not SessionCommandType.ACTIVATE_QUEST:
            return _noncommit(
                BuildNonCommitReason.REDUCER_UNAVAILABLE,
                f"{intent.command_type.value}_REDUCER_UNAVAILABLE",
            )
        payload = _reconstruct(intent.payload, ActivateQuestPayload)
        session = context.session_view
        if not isinstance(session, ControlSessionBuildView):
            raise TypeError("invalid Session view")
        current = session.current_game_snapshot
        if current is None:
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "COMPOSITE_CURRENT_SNAPSHOT_MISSING",
            )
        if not isinstance(current, CandidateGameSnapshot):
            raise TypeError("invalid current Game Snapshot")
        if current.snapshot_schema_version != COMPOSITE_SNAPSHOT_SCHEMA_VERSION:
            return _noncommit(
                BuildNonCommitReason.UNKNOWN_SCHEMA,
                "COMPOSITE_SNAPSHOT_SCHEMA_UNSUPPORTED",
            )
        _revalidate_snapshot(current)
        _validate_scope_and_versions(context, current)
        evidence = _reconstruct(
            session.quest_activation_evidence,
            ControlQuestActivationEvidence,
        )
        context.lifecycle_evidence.validate_for_command(
            SessionCommandType.ACTIVATE_QUEST,
            game_id=session.game_id,
            session_id=session.session_id,
            observed_state_version=session.state_version,
        )
        context.setup_participant_evidence.validate_for_command(
            SessionCommandType.ACTIVATE_QUEST,
            game_id=session.game_id,
            session_id=session.session_id,
            observed_state_version=session.state_version,
        )
        session.game_rule_evidence.validate_for_command(
            SessionCommandType.ACTIVATE_QUEST,
            game_id=session.game_id,
            session_id=session.session_id,
            observed_state_version=session.state_version,
        )
        evidence.validate_for_command(
            SessionCommandType.ACTIVATE_QUEST,
            game_id=session.game_id,
            session_id=session.session_id,
            observed_state_version=session.state_version,
        )
        evidence.validate_payload_binding(payload)
    except (
        AttributeError,
        CompositeSnapshotContractError,
        ControlApplyBuildContextError,
        ControlGameRuleEvidenceError,
        ControlLifecycleEvidenceError,
        ControlQuestEvidenceError,
        ControlSetupParticipantEvidenceError,
        TypeError,
        ValueError,
    ):
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "ACTIVATE_QUEST_EVIDENCE_INVALID",
        )
    if (
        evidence.active_rule_set_reference
        != current.game_rules.committed_rule_set_reference
        or evidence.current_active_quest_id != current.quest.active_quest_id
        or evidence.current_public_state_reference
        != current.quest.committed_public_state_reference
        or evidence.current_hidden_state_reference
        != current.hidden_state.committed_state_reference
        or evidence.game_rule_version != current.game_rules.domain_version
        or evidence.quest_version != current.quest.domain_version
        or evidence.hidden_state_version != current.hidden_state.domain_version
    ):
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "ACTIVATE_QUEST_EVIDENCE_MISMATCH",
        )
    return current, evidence, payload


def _compose(
    context: ControlApplyBuildContext,
    current: CandidateGameSnapshot,
    evidence: ControlQuestActivationEvidence,
    decision: QuestActivationAccepted,
) -> BuildPlanReady | BuildNonCommit:
    session = context.session_view
    envelope = context.envelope
    try:
        candidate = CandidateGameSnapshot(
            game_id=current.game_id,
            session_id=current.session_id,
            group_id=current.group_id,
            dm_participant_id=current.dm_participant_id,
            status=current.status,
            current_phase=current.current_phase,
            state_version=current.state_version + 1,
            last_applied_sequence_no=envelope.event_sequence_no,
            snapshot_schema_version=current.snapshot_schema_version,
            lifecycle=current.lifecycle,
            phase=current.phase,
            setup=current.setup,
            participants=current.participants,
            game_rules=current.game_rules,
            quest=QuestSnapshotSlice(
                schema_version=current.quest.schema_version,
                domain_version=current.quest.domain_version + 1,
                active_quest_id=decision.resulting_state.active_quest_id,
                source_rule_set_reference=(
                    decision.resulting_state.source_rule_set_reference
                ),
                committed_public_state_reference=(
                    decision.resulting_state.committed_public_state_reference
                ),
            ),
            hidden_state=HiddenGameStateSlice(
                schema_version=current.hidden_state.schema_version,
                domain_version=current.hidden_state.domain_version + 1,
                committed_state_reference=(
                    evidence.resulting_hidden_state_reference
                ),
            ),
        )
        mutation = QuestActivationMutation(
            mutation_type=QuestMutationType.ACTIVATE_QUEST,
            quest_id=evidence.quest_id,
            source_rule_set_reference=evidence.active_rule_set_reference,
            public_state_reference=evidence.resulting_public_state_reference,
            current_hidden_state_reference=evidence.current_hidden_state_reference,
            resulting_hidden_state_reference=(
                evidence.resulting_hidden_state_reference
            ),
            expected_game_rule_version=evidence.game_rule_version,
            expected_quest_version=evidence.quest_version,
            resulting_quest_version=evidence.quest_version + 1,
            expected_hidden_state_version=evidence.hidden_state_version,
            resulting_hidden_state_version=evidence.hidden_state_version + 1,
            provenance_reference=evidence.provenance_reference,
        )
        event = _result_event(
            context,
            GameEventType.QUEST_ACTIVATED,
            QuestActivatedPayload(
                command_id=envelope.command_id,
                operation_id=envelope.operation_id,
                input_event_id=envelope.event.event_id,
                result_code="QUEST_ACTIVATED",
                result_state_version=candidate.state_version,
                quest_id=evidence.quest_id,
                public_state_reference=evidence.resulting_public_state_reference,
                quest_domain_version=candidate.quest.domain_version,
            ),
        )
        plan = ControlApplyPlan(
            game_id=session.game_id,
            session_id=session.session_id,
            group_id=session.group_id,
            command_id=envelope.command_id,
            command_type=SessionCommandType.ACTIVATE_QUEST,
            operation_id=envelope.operation_id,
            operation_claim_id=context.claim.claim_id,
            input_event_id=envelope.event.event_id,
            input_sequence_no=envelope.event_sequence_no,
            expected_state_version=session.state_version,
            expected_cursor=session.last_applied_sequence_no,
            expected_binding_version=envelope.requester_binding_version,
            candidate_snapshot=candidate,
            participant_mutations=(),
            setup_mutations=(),
            ownership_intent=OwnershipIntent(
                OwnershipIntentType.UNCHANGED,
                None,
                None,
            ),
            result_events=(event,),
            operation_terminal_state=ControlOperationStatus.SUCCESS,
            lifecycle_evidence=context.lifecycle_evidence,
            setup_participant_evidence=context.setup_participant_evidence,
            game_rule_evidence=session.game_rule_evidence,
            quest_mutations=(mutation,),
            quest_activation_evidence=evidence,
        )
    except (
        AttributeError,
        CompositeSnapshotContractError,
        ControlQuestEvidenceError,
        TypeError,
        ValueError,
    ):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "ACTIVATE_QUEST_COMPOSITION_INVALID",
        )
    return BuildPlanReady(plan)


def _reject(
    context: ControlApplyBuildContext,
    reason: QuestControlRejectReason,
) -> BuildReject:
    session = context.session_view
    envelope = context.envelope
    event = _result_event(
        context,
        GameEventType.SESSION_CONTROL_REJECTED,
        SessionControlRejectedPayload(
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
        ControlRejectPlan(
            game_id=session.game_id,
            session_id=session.session_id,
            group_id=session.group_id,
            command_id=envelope.command_id,
            command_type=SessionCommandType.ACTIVATE_QUEST,
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


def _validate_scope_and_versions(
    context: ControlApplyBuildContext,
    current: CandidateGameSnapshot,
) -> None:
    session = context.session_view
    event = context.envelope.event
    expected_scope = (
        session.game_id,
        session.session_id,
        session.group_id,
        session.dm_participant_id,
    )
    if (
        current.game_id,
        current.session_id,
        current.group_id,
        current.dm_participant_id,
    ) != expected_scope or (event.game_id, event.session_id) != expected_scope[:2]:
        raise ValueError("Quest context scope mismatch")
    if (
        current.state_version != session.state_version
        or current.last_applied_sequence_no > session.last_applied_sequence_no
        or context.envelope.event_sequence_no
        != session.last_applied_sequence_no + 1
    ):
        raise ValueError("Quest context version or cursor mismatch")


def _revalidate_snapshot(current: CandidateGameSnapshot) -> CandidateGameSnapshot:
    records = current.participants.participants
    if not isinstance(records, tuple):
        raise TypeError("participants must be a tuple")
    return CandidateGameSnapshot(
        game_id=current.game_id,
        session_id=current.session_id,
        group_id=current.group_id,
        dm_participant_id=current.dm_participant_id,
        status=current.status,
        current_phase=current.current_phase,
        state_version=current.state_version,
        last_applied_sequence_no=current.last_applied_sequence_no,
        snapshot_schema_version=current.snapshot_schema_version,
        lifecycle=_reconstruct(current.lifecycle, LifecycleSnapshotSlice),
        phase=_reconstruct(current.phase, PhaseSnapshotSlice),
        setup=_reconstruct(current.setup, SetupSnapshotSlice),
        participants=_reconstruct(
            current.participants,
            ParticipantSnapshotSlice,
            participants=tuple(
                _reconstruct(record, ParticipantSnapshotRecord)
                for record in records
            ),
        ),
        game_rules=_reconstruct(current.game_rules, GameRuleSnapshotSlice),
        quest=_reconstruct(current.quest, QuestSnapshotSlice),
        hidden_state=_reconstruct(current.hidden_state, HiddenGameStateSlice),
    )


def _result_event(
    context: ControlApplyBuildContext,
    event_type: GameEventType,
    payload: ControlResultPayload,
) -> GameEvent:
    seed = context.result_event_seed
    return GameEvent(
        event_id=seed.derive_event_id(event_type, ordinal=1),
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


def _reconstruct(value: object, expected_type: type, **overrides: object):
    if not isinstance(value, expected_type):
        raise TypeError(f"value must be a {expected_type.__name__}")
    arguments = {
        field.name: getattr(value, field.name)
        for field in fields(expected_type)
    }
    arguments.update(overrides)
    return expected_type(**arguments)


def _noncommit(
    reason: BuildNonCommitReason,
    detail_code: str,
) -> BuildNonCommit:
    return BuildNonCommit(reason, detail_code)
