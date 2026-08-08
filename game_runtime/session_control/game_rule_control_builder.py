"""Pure composite ApplyPlan builder for ACTIVATE_RULE_SET."""

from __future__ import annotations

from dataclasses import fields
from enum import Enum
from typing import Callable

from game_runtime.event import (
    ClueRevealedPayload,
    GameEvent,
    GameEventSource,
    GameEventType,
    RuleSetActivatedPayload,
    SessionControlRejectedPayload,
)
from game_runtime.event.control_payloads import (
    CONTROL_RESULT_SCHEMA_VERSION,
    CONTROL_RESULT_VISIBILITY,
    ControlResultPayload,
)
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control.apply_contract import (
    ClueRevealMutation,
    ControlApplyPlan,
    ControlOperationClaim,
    ControlRejectPlan,
    GameRuleMutation,
    GameRuleMutationType,
    OwnershipIntent,
    OwnershipIntentType,
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
    ActivateRuleSetPayload,
    RevealCluePayload,
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
from game_runtime.session_control.game_rule_evidence import (
    ClueRevealDisposition,
    ControlClueRevealEvidence,
    ControlGameRuleApplyEvidence,
    ControlGameRuleEvidenceError,
    ControlRuleSetActivationEvidence,
)
from game_runtime.session_control.clue_reveal_transition import (
    ClueRevealAccepted,
    ClueRevealContractError,
    ClueRevealRejected,
    ClueRevealRejectReason,
    ClueRevealRequest,
    ClueRevealState,
    transition_clue_reveal,
)
from game_runtime.session_control.game_rule_transition import (
    GameRuleActivationAccepted,
    GameRuleActivationContractError,
    GameRuleActivationRejected,
    GameRuleActivationRejectReason,
    GameRuleActivationRequest,
    GameRuleActivationState,
    transition_game_rule_activation,
)
from game_runtime.session_control.lifecycle_evidence import (
    ControlLifecycleApplyEvidence,
    ControlLifecycleEvidenceError,
)
from game_runtime.session_control.operation import ControlOperationStatus
from game_runtime.session_control.setup_participant_evidence import (
    ControlSetupParticipantApplyEvidence,
    ControlSetupParticipantEvidenceError,
)


class GameRuleControlRejectReason(str, Enum):
    INVALID_LIFECYCLE = "INVALID_LIFECYCLE"
    SETUP_NOT_READY = "SETUP_NOT_READY"
    RULE_SET_ALREADY_ACTIVE = "RULE_SET_ALREADY_ACTIVE"
    RULE_SET_CONFLICT = "RULE_SET_CONFLICT"
    INVALID_PHASE = "INVALID_PHASE"
    RULE_SET_NOT_ACTIVE = "RULE_SET_NOT_ACTIVE"
    CLUE_ALREADY_REVEALED = "CLUE_ALREADY_REVEALED"
    CLUE_NOT_FOUND = "CLUE_NOT_FOUND"
    CLUE_NOT_REVEALABLE = "CLUE_NOT_REVEALABLE"


class GameRuleControlApplyPlanBuilder:
    """Build one deterministic rule-set activation plan."""

    __slots__ = ()

    def build(self, context: ControlApplyBuildContext) -> BuildOutcome:
        if not isinstance(context, ControlApplyBuildContext):
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "CONTROL_APPLY_CONTEXT_TYPE_INVALID",
            )
        if not _revalidate_consumed_context(context):
            return _invalid_consumed_context_outcome(context)

        intent = context.command_intent
        if not isinstance(intent, CanonicalControlCommandIntent):
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "CONTROL_COMMAND_INTENT_INVALID",
            )
        if intent.intent_schema_version != 1:
            return _noncommit(
                BuildNonCommitReason.UNKNOWN_SCHEMA,
                "CONTROL_COMMAND_INTENT_SCHEMA_UNSUPPORTED",
            )
        if intent.command_type is SessionCommandType.REVEAL_CLUE:
            return _build_clue_reveal(context, self._reject)
        if intent.command_type is not SessionCommandType.ACTIVATE_RULE_SET:
            command_type = intent.command_type
            return _noncommit(
                (
                    BuildNonCommitReason.REDUCER_UNAVAILABLE
                    if isinstance(command_type, SessionCommandType)
                    else BuildNonCommitReason.INVALID_CONTEXT
                ),
                (
                    f"{command_type.value}_REDUCER_UNAVAILABLE"
                    if isinstance(command_type, SessionCommandType)
                    else "CONTROL_COMMAND_TYPE_INVALID"
                ),
            )
        payload = intent.payload
        if type(payload) is not ActivateRuleSetPayload:
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "ACTIVATE_RULE_SET_INPUT_INVALID",
            )

        session = context.session_view
        current = session.current_game_snapshot
        if current is None:
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "COMPOSITE_CURRENT_SNAPSHOT_MISSING",
            )
        if not isinstance(current, CandidateGameSnapshot):
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "COMPOSITE_CURRENT_SNAPSHOT_INVALID",
            )
        if current.snapshot_schema_version != COMPOSITE_SNAPSHOT_SCHEMA_VERSION:
            return _noncommit(
                BuildNonCommitReason.UNKNOWN_SCHEMA,
                "COMPOSITE_SNAPSHOT_SCHEMA_UNSUPPORTED",
            )
        try:
            _revalidate_candidate_snapshot(current)
            binding_failure = _validate_composite_bindings(context, current)
        except (
            AttributeError,
            CompositeSnapshotContractError,
            TypeError,
            ValueError,
        ):
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "COMPOSITE_CURRENT_SNAPSHOT_INVALID",
            )
        if binding_failure is not None:
            return binding_failure

        evidence_result = _validated_activation_evidence(
            context=context,
            current=current,
            payload=payload,
        )
        if isinstance(evidence_result, BuildNonCommit):
            return evidence_result
        evidence = evidence_result

        if (
            session.status is not GameSessionStatus.CREATED
            or session.current_phase is not GamePhase.LOBBY
        ):
            return self._reject(
                context,
                GameRuleControlRejectReason.INVALID_LIFECYCLE,
            )
        if current.setup.domain_version == 0:
            return self._reject(
                context,
                GameRuleControlRejectReason.SETUP_NOT_READY,
            )

        try:
            decision = transition_game_rule_activation(
                GameRuleActivationRequest(
                    current_state=GameRuleActivationState(
                        committed_rule_set_reference=(
                            current.game_rules.committed_rule_set_reference
                        ),
                        opaque_hidden_state_reference=(
                            current.hidden_state.committed_state_reference
                        ),
                    ),
                    requested_state=GameRuleActivationState(
                        committed_rule_set_reference=(
                            evidence.committed_rule_set_reference
                        ),
                        opaque_hidden_state_reference=(
                            evidence.opaque_hidden_state_reference
                        ),
                    ),
                )
            )
        except (GameRuleActivationContractError, TypeError, ValueError):
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "ACTIVATE_RULE_SET_TRANSITION_INVALID",
            )
        if isinstance(decision, GameRuleActivationRejected):
            rejection = {
                GameRuleActivationRejectReason.RULE_SET_ALREADY_ACTIVE:
                    GameRuleControlRejectReason.RULE_SET_ALREADY_ACTIVE,
                GameRuleActivationRejectReason.RULE_SET_CONFLICT:
                    GameRuleControlRejectReason.RULE_SET_CONFLICT,
            }.get(decision.reason)
            if rejection is None:
                return _noncommit(
                    BuildNonCommitReason.INVALID_CONTEXT,
                    "ACTIVATE_RULE_SET_TRANSITION_INVALID",
                )
            return self._reject(context, rejection)
        if not isinstance(decision, GameRuleActivationAccepted):
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "ACTIVATE_RULE_SET_TRANSITION_INVALID",
            )
        return _compose_game_rule_apply_plan(
            context=context,
            current=current,
            decision=decision,
            evidence=evidence,
        )

    def _reject(
        self,
        context: ControlApplyBuildContext,
        reason: GameRuleControlRejectReason,
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


def _build_clue_reveal(
    context: ControlApplyBuildContext,
    reject: Callable[
        [ControlApplyBuildContext, GameRuleControlRejectReason], BuildReject
    ],
) -> BuildOutcome:
    intent = context.command_intent
    payload = intent.payload
    if type(payload) is not RevealCluePayload:
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "REVEAL_CLUE_INPUT_INVALID",
        )
    session = context.session_view
    current = session.current_game_snapshot
    if current is None:
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "COMPOSITE_CURRENT_SNAPSHOT_MISSING",
        )
    if not isinstance(current, CandidateGameSnapshot):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "COMPOSITE_CURRENT_SNAPSHOT_INVALID",
        )
    if current.snapshot_schema_version != COMPOSITE_SNAPSHOT_SCHEMA_VERSION:
        return _noncommit(
            BuildNonCommitReason.UNKNOWN_SCHEMA,
            "COMPOSITE_SNAPSHOT_SCHEMA_UNSUPPORTED",
        )
    try:
        _revalidate_candidate_snapshot(current)
        binding_failure = _validate_composite_bindings(context, current)
    except (
        AttributeError,
        CompositeSnapshotContractError,
        TypeError,
        ValueError,
    ):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "COMPOSITE_CURRENT_SNAPSHOT_INVALID",
        )
    if binding_failure is not None:
        return binding_failure
    evidence_result = _validated_reveal_evidence(
        context=context,
        current=current,
        payload=payload,
    )
    if isinstance(evidence_result, BuildNonCommit):
        return evidence_result
    evidence = evidence_result

    if session.status is not GameSessionStatus.RUNNING:
        return reject(context, GameRuleControlRejectReason.INVALID_LIFECYCLE)
    if session.current_phase is not GamePhase.EXPLORATION:
        return reject(context, GameRuleControlRejectReason.INVALID_PHASE)
    try:
        current_state = ClueRevealState(
            rule_set_reference=current.game_rules.committed_rule_set_reference,
            disclosure_state_reference=(
                current.game_rules.committed_disclosure_state_reference
            ),
            hidden_state_reference=current.hidden_state.committed_state_reference,
        )
        candidate_state = (
            ClueRevealState(
                rule_set_reference=evidence.active_rule_set_reference,
                disclosure_state_reference=(
                    evidence.resulting_disclosure_state_reference
                ),
                hidden_state_reference=evidence.resulting_hidden_state_reference,
            )
            if evidence.disposition is ClueRevealDisposition.AVAILABLE
            else None
        )
        decision = transition_clue_reveal(
            ClueRevealRequest(
                clue_id=payload.clue_id,
                current_state=current_state,
                candidate_state=candidate_state,
                disposition=evidence.disposition,
            )
        )
    except (ClueRevealContractError, TypeError, ValueError):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "REVEAL_CLUE_TRANSITION_INVALID",
        )
    if isinstance(decision, ClueRevealRejected):
        reason = {
            ClueRevealRejectReason.RULE_SET_NOT_ACTIVE:
                GameRuleControlRejectReason.RULE_SET_NOT_ACTIVE,
            ClueRevealRejectReason.CLUE_ALREADY_REVEALED:
                GameRuleControlRejectReason.CLUE_ALREADY_REVEALED,
            ClueRevealRejectReason.CLUE_NOT_FOUND:
                GameRuleControlRejectReason.CLUE_NOT_FOUND,
            ClueRevealRejectReason.CLUE_NOT_REVEALABLE:
                GameRuleControlRejectReason.CLUE_NOT_REVEALABLE,
        }.get(decision.reason)
        if reason is None:
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "REVEAL_CLUE_TRANSITION_INVALID",
            )
        return reject(context, reason)
    if not isinstance(decision, ClueRevealAccepted):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "REVEAL_CLUE_TRANSITION_INVALID",
        )
    return _compose_clue_reveal_apply_plan(
        context=context,
        current=current,
        decision=decision,
        evidence=evidence,
    )


def _validated_reveal_evidence(
    *,
    context: ControlApplyBuildContext,
    current: CandidateGameSnapshot,
    payload: RevealCluePayload,
) -> ControlClueRevealEvidence | BuildNonCommit:
    session = context.session_view
    bundle = session.game_rule_evidence
    try:
        if not isinstance(bundle, ControlGameRuleApplyEvidence):
            raise ControlGameRuleEvidenceError("invalid Game Rule evidence bundle")
        bundle.validate_for_command(
            SessionCommandType.REVEAL_CLUE,
            game_id=session.game_id,
            session_id=session.session_id,
            observed_state_version=session.state_version,
        )
        bundle.validate_payload_binding(SessionCommandType.REVEAL_CLUE, payload)
        evidence = bundle.clue_reveal
        if not isinstance(evidence, ControlClueRevealEvidence):
            raise ControlGameRuleEvidenceError("clue reveal evidence is incomplete")
        evidence = _reconstruct(evidence, ControlClueRevealEvidence)
    except (
        AttributeError,
        ControlGameRuleEvidenceError,
        TypeError,
        ValueError,
    ):
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "REVEAL_CLUE_EVIDENCE_INVALID",
        )
    if (
        evidence.game_rule_version != current.game_rules.domain_version
        or evidence.hidden_state_version != current.hidden_state.domain_version
        or evidence.active_rule_set_reference
        != current.game_rules.committed_rule_set_reference
        or evidence.current_disclosure_state_reference
        != current.game_rules.committed_disclosure_state_reference
        or evidence.current_hidden_state_reference
        != current.hidden_state.committed_state_reference
    ):
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "REVEAL_CLUE_EVIDENCE_MISMATCH",
        )
    return evidence


def _compose_clue_reveal_apply_plan(
    *,
    context: ControlApplyBuildContext,
    current: CandidateGameSnapshot,
    decision: ClueRevealAccepted,
    evidence: ControlClueRevealEvidence,
) -> BuildPlanReady | BuildNonCommit:
    if (
        current is not context.session_view.current_game_snapshot
        or decision.clue_id != context.command_intent.payload.clue_id
        or evidence.clue_id != decision.clue_id
    ):
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "REVEAL_CLUE_COMPOSITION_INVALID",
        )
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
            game_rules=GameRuleSnapshotSlice(
                schema_version=current.game_rules.schema_version,
                domain_version=current.game_rules.domain_version + 1,
                committed_rule_set_reference=(
                    decision.resulting_state.rule_set_reference
                ),
                committed_disclosure_state_reference=(
                    decision.resulting_state.disclosure_state_reference
                ),
            ),
            quest=current.quest,
            hidden_state=HiddenGameStateSlice(
                schema_version=current.hidden_state.schema_version,
                domain_version=current.hidden_state.domain_version + 1,
                committed_state_reference=(
                    decision.resulting_state.hidden_state_reference
                ),
            ),
        )
        mutation = ClueRevealMutation(
            mutation_type=GameRuleMutationType.REVEAL_CLUE,
            clue_id=evidence.clue_id,
            active_rule_set_reference=evidence.active_rule_set_reference,
            public_disclosure_reference=evidence.public_disclosure_reference,
            current_disclosure_state_reference=(
                evidence.current_disclosure_state_reference
            ),
            resulting_disclosure_state_reference=(
                evidence.resulting_disclosure_state_reference
            ),
            current_hidden_state_reference=evidence.current_hidden_state_reference,
            resulting_hidden_state_reference=(
                evidence.resulting_hidden_state_reference
            ),
            expected_game_rule_version=evidence.game_rule_version,
            resulting_game_rule_version=evidence.game_rule_version + 1,
            expected_hidden_state_version=evidence.hidden_state_version,
            resulting_hidden_state_version=evidence.hidden_state_version + 1,
            provenance_reference=evidence.provenance_reference,
        )
        event = _result_event(
            context,
            event_type=GameEventType.CLUE_REVEALED,
            ordinal=1,
            payload=ClueRevealedPayload(
                command_id=envelope.command_id,
                operation_id=envelope.operation_id,
                input_event_id=envelope.event.event_id,
                result_code="CLUE_REVEALED",
                result_state_version=candidate.state_version,
                clue_id=evidence.clue_id,
                public_disclosure_reference=evidence.public_disclosure_reference,
                game_rule_domain_version=candidate.game_rules.domain_version,
            ),
        )
        plan = ControlApplyPlan(
            game_id=session.game_id,
            session_id=session.session_id,
            group_id=session.group_id,
            command_id=envelope.command_id,
            command_type=SessionCommandType.REVEAL_CLUE,
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
                intent_type=OwnershipIntentType.UNCHANGED,
                expected_generation=None,
                resulting_generation=None,
            ),
            result_events=(event,),
            operation_terminal_state=ControlOperationStatus.SUCCESS,
            lifecycle_evidence=context.lifecycle_evidence,
            setup_participant_evidence=context.setup_participant_evidence,
            game_rule_mutations=(mutation,),
            game_rule_evidence=context.session_view.game_rule_evidence,
        )
    except (
        AttributeError,
        CompositeSnapshotContractError,
        ControlGameRuleEvidenceError,
        TypeError,
        ValueError,
    ):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "REVEAL_CLUE_COMPOSITION_INVALID",
        )
    return BuildPlanReady(plan=plan)


def _compose_game_rule_apply_plan(
    *,
    context: ControlApplyBuildContext,
    current: CandidateGameSnapshot,
    decision: GameRuleActivationAccepted,
    evidence: ControlRuleSetActivationEvidence,
) -> BuildPlanReady | BuildNonCommit:
    """Compose a complete activation plan only from fully revalidated values."""

    if (
        not isinstance(context, ControlApplyBuildContext)
        or not isinstance(current, CandidateGameSnapshot)
        or not isinstance(decision, GameRuleActivationAccepted)
        or not isinstance(evidence, ControlRuleSetActivationEvidence)
        or current is not context.session_view.current_game_snapshot
        or not _revalidate_consumed_context(context)
    ):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "ACTIVATE_RULE_SET_COMPOSITION_INVALID",
        )
    payload = context.command_intent.payload
    if (
        context.command_intent.command_type
        is not SessionCommandType.ACTIVATE_RULE_SET
        or type(payload) is not ActivateRuleSetPayload
    ):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "ACTIVATE_RULE_SET_COMPOSITION_INVALID",
        )
    try:
        _revalidate_candidate_snapshot(current)
        binding_failure = _validate_composite_bindings(context, current)
    except (
        AttributeError,
        CompositeSnapshotContractError,
        TypeError,
        ValueError,
    ):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "ACTIVATE_RULE_SET_COMPOSITION_INVALID",
        )
    if binding_failure is not None:
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "ACTIVATE_RULE_SET_COMPOSITION_INVALID",
        )
    validated = _validated_activation_evidence(
        context=context,
        current=current,
        payload=payload,
    )
    if isinstance(validated, BuildNonCommit) or validated != evidence:
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "ACTIVATE_RULE_SET_COMPOSITION_INVALID",
        )
    if (
        context.session_view.status is not GameSessionStatus.CREATED
        or context.session_view.current_phase is not GamePhase.LOBBY
        or current.setup.domain_version == 0
    ):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "ACTIVATE_RULE_SET_COMPOSITION_INVALID",
        )
    try:
        expected_decision = transition_game_rule_activation(
            GameRuleActivationRequest(
                current_state=GameRuleActivationState(
                    current.game_rules.committed_rule_set_reference,
                    current.hidden_state.committed_state_reference,
                ),
                requested_state=GameRuleActivationState(
                    evidence.committed_rule_set_reference,
                    evidence.opaque_hidden_state_reference,
                ),
            )
        )
    except (GameRuleActivationContractError, TypeError, ValueError):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "ACTIVATE_RULE_SET_COMPOSITION_INVALID",
        )
    if decision != expected_decision:
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "ACTIVATE_RULE_SET_COMPOSITION_INVALID",
        )

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
            game_rules=GameRuleSnapshotSlice(
                schema_version=current.game_rules.schema_version,
                domain_version=current.game_rules.domain_version + 1,
                committed_rule_set_reference=(
                    decision.resulting_state.committed_rule_set_reference
                ),
                committed_disclosure_state_reference=(
                    evidence.initial_disclosure_state_reference
                ),
            ),
            quest=current.quest,
            hidden_state=HiddenGameStateSlice(
                schema_version=current.hidden_state.schema_version,
                domain_version=current.hidden_state.domain_version + 1,
                committed_state_reference=(
                    decision.resulting_state.opaque_hidden_state_reference
                ),
            ),
        )
        mutation = GameRuleMutation(
            mutation_type=GameRuleMutationType.ACTIVATE_RULE_SET,
            manifest_reference=payload.manifest_reference,
            committed_rule_set_reference=evidence.committed_rule_set_reference,
            committed_disclosure_state_reference=(
                evidence.initial_disclosure_state_reference
            ),
            opaque_hidden_state_reference=evidence.opaque_hidden_state_reference,
            expected_game_rule_version=evidence.rule_set_version,
            resulting_game_rule_version=evidence.rule_set_version + 1,
            expected_hidden_state_version=evidence.hidden_state_version,
            resulting_hidden_state_version=evidence.hidden_state_version + 1,
            provenance_reference=evidence.provenance_reference,
        )
        event = _result_event(
            context,
            event_type=GameEventType.RULE_SET_ACTIVATED,
            ordinal=1,
            payload=RuleSetActivatedPayload(
                command_id=envelope.command_id,
                operation_id=envelope.operation_id,
                input_event_id=envelope.event.event_id,
                result_code="RULE_SET_ACTIVATED",
                result_state_version=candidate.state_version,
                manifest_reference=payload.manifest_reference,
                rule_set_domain_version=candidate.game_rules.domain_version,
                hidden_state_domain_version=candidate.hidden_state.domain_version,
            ),
        )
        plan = ControlApplyPlan(
            game_id=session.game_id,
            session_id=session.session_id,
            group_id=session.group_id,
            command_id=envelope.command_id,
            command_type=SessionCommandType.ACTIVATE_RULE_SET,
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
                intent_type=OwnershipIntentType.UNCHANGED,
                expected_generation=None,
                resulting_generation=None,
            ),
            result_events=(event,),
            operation_terminal_state=ControlOperationStatus.SUCCESS,
            lifecycle_evidence=context.lifecycle_evidence,
            setup_participant_evidence=context.setup_participant_evidence,
            game_rule_mutations=(mutation,),
            game_rule_evidence=context.session_view.game_rule_evidence,
        )
    except (
        AttributeError,
        CompositeSnapshotContractError,
        ControlApplyBuildContextError,
        ControlGameRuleEvidenceError,
        TypeError,
        ValueError,
    ):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "ACTIVATE_RULE_SET_COMPOSITION_INVALID",
        )
    if (
        plan.candidate_snapshot is not candidate
        or candidate.lifecycle is not current.lifecycle
        or candidate.phase is not current.phase
        or candidate.setup is not current.setup
        or candidate.participants is not current.participants
        or plan.participant_mutations
        or plan.setup_mutations
        or plan.game_rule_mutations != (mutation,)
        or plan.result_events != (event,)
        or plan.ownership_intent
        != OwnershipIntent(OwnershipIntentType.UNCHANGED, None, None)
    ):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "ACTIVATE_RULE_SET_COMPOSITION_INVALID",
        )
    return BuildPlanReady(plan=plan)


def _validated_activation_evidence(
    *,
    context: ControlApplyBuildContext,
    current: CandidateGameSnapshot,
    payload: ActivateRuleSetPayload,
) -> ControlRuleSetActivationEvidence | BuildNonCommit:
    session = context.session_view
    bundle = session.game_rule_evidence
    try:
        if not isinstance(bundle, ControlGameRuleApplyEvidence):
            raise ControlGameRuleEvidenceError("invalid Game Rule evidence bundle")
        bundle.validate_for_command(
            SessionCommandType.ACTIVATE_RULE_SET,
            game_id=session.game_id,
            session_id=session.session_id,
            observed_state_version=session.state_version,
        )
        bundle.validate_payload_binding(
            SessionCommandType.ACTIVATE_RULE_SET,
            payload,
        )
        evidence = bundle.rule_set_activation
        if not isinstance(evidence, ControlRuleSetActivationEvidence):
            raise ControlGameRuleEvidenceError(
                "activation evidence is incomplete"
            )
        evidence = _reconstruct(
            evidence,
            ControlRuleSetActivationEvidence,
        )
    except (
        AttributeError,
        ControlGameRuleEvidenceError,
        TypeError,
        ValueError,
    ):
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "ACTIVATE_RULE_SET_EVIDENCE_INVALID",
        )
    setup_matches = (
        evidence.setup_version == 0
        if current.setup.domain_version == 0
        else (
            evidence.setup_manifest_reference
            == current.setup.manifest_reference
            and evidence.setup_version == current.setup.domain_version
        )
    )
    if (
        not setup_matches
        or evidence.rule_set_version != current.game_rules.domain_version
        or evidence.hidden_state_version != current.hidden_state.domain_version
    ):
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "ACTIVATE_RULE_SET_EVIDENCE_MISMATCH",
        )
    return evidence


def _validate_composite_bindings(
    context: ControlApplyBuildContext,
    current: CandidateGameSnapshot,
) -> BuildNonCommit | None:
    session = context.session_view
    event = context.envelope.event
    if (
        current.game_id,
        current.session_id,
        current.group_id,
        current.dm_participant_id,
    ) != (
        session.game_id,
        session.session_id,
        session.group_id,
        session.dm_participant_id,
    ) or (event.game_id, event.session_id) != (
        session.game_id,
        session.session_id,
    ):
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "COMPOSITE_SCOPE_BINDING_MISMATCH",
        )
    if (
        current.status is not session.status
        or current.current_phase is not session.current_phase
        or current.lifecycle.status is not session.status
        or current.phase.phase is not session.current_phase
    ):
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "COMPOSITE_LIFECYCLE_BINDING_MISMATCH",
        )
    if (
        current.state_version != session.state_version
        or context.envelope.observed_state_version != session.state_version
        or event.observed_state_version != session.state_version
        or context.ownership_evidence.observed_state_version
        != session.state_version
    ):
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "COMPOSITE_STATE_VERSION_MISMATCH",
        )
    if (
        current.last_applied_sequence_no != session.last_applied_sequence_no
        or context.envelope.event_sequence_no
        != session.last_applied_sequence_no + 1
    ):
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "COMPOSITE_CURSOR_BINDING_MISMATCH",
        )
    try:
        expected_participants = tuple(
            ParticipantSnapshotRecord(
                participant_id=view.participant_id,
                participant_type=view.participant_type,
                membership_state=view.membership_state,
                character_id=view.character_id,
                binding_version=view.binding_version,
            )
            for view in sorted(
                context.participant_views,
                key=lambda item: item.participant_id,
            )
        )
    except (CompositeSnapshotContractError, TypeError, ValueError):
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "COMPOSITE_PARTICIPANT_BINDING_MISMATCH",
        )
    if current.participants.participants != expected_participants:
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "COMPOSITE_PARTICIPANT_BINDING_MISMATCH",
        )
    setup = current.setup
    view = context.setup_view
    if setup.domain_version == 0:
        setup_matches = (
            view is None
            and setup.script_id is None
            and setup.public_name is None
            and setup.manifest_reference is None
            and setup.manifest_version is None
        )
    else:
        setup_matches = (
            isinstance(view, ControlSetupBuildView)
            and view.game_id == current.game_id
            and view.session_id == current.session_id
            and view.script_id == setup.script_id
            and view.public_name == setup.public_name
            and view.manifest_reference == setup.manifest_reference
            and view.setup_version == setup.domain_version
            and setup.manifest_version is not None
        )
    if not setup_matches:
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "COMPOSITE_SETUP_BINDING_MISMATCH",
        )
    ownership = context.ownership_evidence
    if (
        ownership.game_id,
        ownership.session_id,
        ownership.group_id,
        ownership.active_generation,
    ) != (
        session.game_id,
        session.session_id,
        session.group_id,
        None,
    ):
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "ACTIVATE_RULE_SET_OWNERSHIP_INVALID",
        )
    return None


def _revalidate_consumed_context(
    context: ControlApplyBuildContext,
) -> bool:
    try:
        if (
            not isinstance(context.envelope, ControlEventDeliveryEnvelope)
            or not isinstance(context.envelope.event, GameEvent)
            or not isinstance(context.claim, ControlOperationClaim)
            or not isinstance(
                context.command_intent,
                CanonicalControlCommandIntent,
            )
            or not isinstance(context.session_view, ControlSessionBuildView)
            or not isinstance(context.participant_views, tuple)
            or (
                context.setup_view is not None
                and not isinstance(context.setup_view, ControlSetupBuildView)
            )
            or not isinstance(
                context.ownership_evidence,
                ControlOwnershipBuildEvidence,
            )
            or not isinstance(
                context.result_event_seed,
                ControlResultEventSeed,
            )
            or not isinstance(
                context.lifecycle_evidence,
                ControlLifecycleApplyEvidence,
            )
            or not isinstance(
                context.setup_participant_evidence,
                ControlSetupParticipantApplyEvidence,
            )
        ):
            return False
        event = _reconstruct(context.envelope.event, GameEvent)
        envelope = _reconstruct(
            context.envelope,
            ControlEventDeliveryEnvelope,
            event=event,
        )
        claim = _reconstruct(context.claim, ControlOperationClaim)
        payload = context.command_intent.payload
        payload = _reconstruct(payload, type(payload))
        intent = _reconstruct(
            context.command_intent,
            CanonicalControlCommandIntent,
            payload=payload,
        )
        bundle = context.session_view.game_rule_evidence
        if not isinstance(bundle, ControlGameRuleApplyEvidence):
            return False
        activation = bundle.rule_set_activation
        if activation is not None:
            activation = _reconstruct(
                activation,
                ControlRuleSetActivationEvidence,
            )
        reveal = bundle.clue_reveal
        if reveal is not None:
            reveal = _reconstruct(reveal, ControlClueRevealEvidence)
        bundle = ControlGameRuleApplyEvidence(
            rule_set_activation=activation,
            clue_reveal=reveal,
        )
        session = _reconstruct(
            context.session_view,
            ControlSessionBuildView,
            current_game_snapshot=context.session_view.current_game_snapshot,
            game_rule_evidence=bundle,
        )
        participants = tuple(
            _reconstruct(view, ControlParticipantBuildView)
            for view in context.participant_views
        )
        setup = (
            None
            if context.setup_view is None
            else _reconstruct(context.setup_view, ControlSetupBuildView)
        )
        ownership = _reconstruct(
            context.ownership_evidence,
            ControlOwnershipBuildEvidence,
        )
        seed = _reconstruct(
            context.result_event_seed,
            ControlResultEventSeed,
        )
        lifecycle_evidence = _reconstruct_evidence_bundle(
            context.lifecycle_evidence,
            ControlLifecycleApplyEvidence,
        )
        setup_participant_evidence = _reconstruct_evidence_bundle(
            context.setup_participant_evidence,
            ControlSetupParticipantApplyEvidence,
        )
        ControlApplyBuildContext(
            envelope=envelope,
            claim=claim,
            command_intent=intent,
            session_view=session,
            participant_views=participants,
            setup_view=setup,
            ownership_evidence=ownership,
            result_event_seed=seed,
            lifecycle_evidence=lifecycle_evidence,
            setup_participant_evidence=setup_participant_evidence,
        )
        lifecycle_evidence.validate_for_command(
            intent.command_type,
            game_id=session.game_id,
            session_id=session.session_id,
            observed_state_version=session.state_version,
        )
        setup_participant_evidence.validate_for_command(
            intent.command_type,
            game_id=session.game_id,
            session_id=session.session_id,
            observed_state_version=session.state_version,
        )
    except (
        AttributeError,
        ControlApplyBuildContextError,
        ControlGameRuleEvidenceError,
        ControlLifecycleEvidenceError,
        ControlSetupParticipantEvidenceError,
        TypeError,
        ValueError,
    ):
        return False
    return True


def _invalid_consumed_context_outcome(
    context: ControlApplyBuildContext,
) -> BuildNonCommit:
    try:
        session = context.session_view
        command_type = context.command_intent.command_type
        if command_type not in {
            SessionCommandType.ACTIVATE_RULE_SET,
            SessionCommandType.REVEAL_CLUE,
        }:
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "CONTROL_APPLY_CONTEXT_INVALID",
            )
        for evidence in (
            context.lifecycle_evidence,
            context.setup_participant_evidence,
        ):
            evidence.validate_for_command(
                command_type,
                game_id=session.game_id,
                session_id=session.session_id,
                observed_state_version=session.state_version,
            )
        bundle = context.session_view.game_rule_evidence
        valid_game_rule_evidence = (
            isinstance(bundle, ControlGameRuleApplyEvidence)
            and (
                (
                    command_type is SessionCommandType.ACTIVATE_RULE_SET
                    and isinstance(
                        bundle.rule_set_activation,
                        ControlRuleSetActivationEvidence,
                    )
                )
                or (
                    command_type is SessionCommandType.REVEAL_CLUE
                    and isinstance(bundle.clue_reveal, ControlClueRevealEvidence)
                )
            )
        )
        if not valid_game_rule_evidence:
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                f"{command_type.value}_EVIDENCE_INVALID",
            )
    except (
        AttributeError,
        ControlLifecycleEvidenceError,
        ControlSetupParticipantEvidenceError,
        TypeError,
        ValueError,
    ):
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            (
                f"{command_type.value}_CONTEXT_EVIDENCE_INVALID"
                if isinstance(command_type, SessionCommandType)
                else "GAME_RULE_CONTEXT_EVIDENCE_INVALID"
            ),
        )
    return _noncommit(
        BuildNonCommitReason.INVALID_CONTEXT,
        "CONTROL_APPLY_CONTEXT_INVALID",
    )


def _revalidate_candidate_snapshot(
    current: CandidateGameSnapshot,
) -> CandidateGameSnapshot:
    original_records = current.participants.participants
    if not isinstance(original_records, tuple):
        raise TypeError("snapshot participants must be a tuple")
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
                for record in original_records
            ),
        ),
        game_rules=_reconstruct(
            current.game_rules,
            GameRuleSnapshotSlice,
        ),
        quest=_reconstruct(current.quest, QuestSnapshotSlice),
        hidden_state=_reconstruct(
            current.hidden_state,
            HiddenGameStateSlice,
        ),
    )


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


def _reconstruct(
    value: object,
    expected_type: type,
    **overrides: object,
) -> object:
    if not isinstance(value, expected_type):
        raise TypeError(f"value must be a {expected_type.__name__}")
    arguments = {
        field.name: getattr(value, field.name)
        for field in fields(expected_type)
    }
    arguments.update(overrides)
    return expected_type(**arguments)


def _reconstruct_evidence_bundle(
    value: object,
    expected_type: type,
) -> object:
    if not isinstance(value, expected_type):
        raise TypeError(f"value must be a {expected_type.__name__}")
    arguments: dict[str, object] = {}
    for field in fields(expected_type):
        item = getattr(value, field.name)
        arguments[field.name] = (
            None if item is None else _reconstruct(item, type(item))
        )
    return expected_type(**arguments)


def _noncommit(
    reason: BuildNonCommitReason,
    detail_code: str,
) -> BuildNonCommit:
    return BuildNonCommit(reason=reason, detail_code=detail_code)
