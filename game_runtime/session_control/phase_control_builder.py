"""Pure composite-native ApplyPlan builder for CHANGE_PHASE."""

from __future__ import annotations

from dataclasses import fields
from enum import Enum

from game_runtime.event import (
    GameEvent,
    GameEventSource,
    GameEventType,
    PhaseChangedPayload,
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
    ControlOperationClaim,
    ControlRejectPlan,
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
    ChangePhasePayload,
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
    ControlPhaseVisibilityIntent,
)
from game_runtime.session_control.operation import ControlOperationStatus
from game_runtime.session_control.phase_transition import (
    PhaseTransitionRejected,
    PhaseTransitionRequest,
    transition_phase,
)
from game_runtime.session_control.setup_participant_evidence import (
    ControlSetupParticipantApplyEvidence,
)


class _PhaseRejectReason(str, Enum):
    STALE_STATE_VERSION = "STALE_STATE_VERSION"
    INVALID_LIFECYCLE_TRANSITION = "INVALID_LIFECYCLE_TRANSITION"
    INVALID_PHASE_TRANSITION = "INVALID_PHASE_TRANSITION"


class PhaseControlApplyPlanBuilder:
    """Build one deterministic composite CHANGE_PHASE plan."""

    __slots__ = ()

    def build(self, context: ControlApplyBuildContext) -> BuildOutcome:
        if not isinstance(context, ControlApplyBuildContext):
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "CONTROL_APPLY_CONTEXT_TYPE_INVALID",
            )
        if not _revalidate_consumed_context(context):
            return _invalid_consumed_context_outcome(context)

        if not isinstance(
            context.command_intent,
            CanonicalControlCommandIntent,
        ):
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "CONTROL_COMMAND_INTENT_INVALID",
            )
        if context.command_intent.intent_schema_version != 1:
            return _noncommit(
                BuildNonCommitReason.UNKNOWN_SCHEMA,
                "CONTROL_COMMAND_INTENT_SCHEMA_UNSUPPORTED",
            )
        command_type = context.command_intent.command_type
        if not isinstance(command_type, SessionCommandType):
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "CONTROL_COMMAND_TYPE_INVALID",
            )
        if command_type is not SessionCommandType.CHANGE_PHASE:
            return _noncommit(
                BuildNonCommitReason.REDUCER_UNAVAILABLE,
                f"{command_type.value}_REDUCER_UNAVAILABLE",
            )
        payload = context.command_intent.payload
        if (
            type(payload) is not ChangePhasePayload
            or not isinstance(payload.target_phase, GamePhase)
        ):
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "CHANGE_PHASE_PAYLOAD_INVALID",
            )

        if not isinstance(
            context.lifecycle_evidence,
            ControlLifecycleApplyEvidence,
        ):
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "CHANGE_PHASE_EVIDENCE_INCOMPLETE",
            )
        if not isinstance(
            context.ownership_evidence,
            ControlOwnershipBuildEvidence,
        ):
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "CHANGE_PHASE_OWNERSHIP_INVALID",
            )

        session = context.session_view
        current_version = session.state_version
        observed_version = context.envelope.observed_state_version
        if current_version < observed_version:
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "COMMAND_VERSION_AHEAD_OF_ACTOR",
            )
        if current_version > observed_version:
            return self._reject(
                context,
                _PhaseRejectReason.STALE_STATE_VERSION,
            )

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
        try:
            _revalidate_candidate_snapshot(current)
        except (
            AttributeError,
            CompositeSnapshotContractError,
            TypeError,
            ValueError,
        ):
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "COMPOSITE_CANDIDATE_INVALID",
            )

        if session.status is not GameSessionStatus.RUNNING:
            return self._reject(
                context,
                _PhaseRejectReason.INVALID_LIFECYCLE_TRANSITION,
            )

        try:
            context.lifecycle_evidence.validate_for_command(
                SessionCommandType.CHANGE_PHASE,
                game_id=session.game_id,
                session_id=session.session_id,
                observed_state_version=current_version,
            )
        except (
            AttributeError,
            ControlLifecycleEvidenceError,
            TypeError,
            ValueError,
        ):
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "CHANGE_PHASE_EVIDENCE_INCOMPLETE",
            )

        visibility = context.lifecycle_evidence.phase_visibility_intent
        try:
            validated_visibility = _reconstruct(
                visibility,
                ControlPhaseVisibilityIntent,
            )
        except (
            AttributeError,
            ControlLifecycleEvidenceError,
            TypeError,
            ValueError,
        ):
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "CHANGE_PHASE_EVIDENCE_INCOMPLETE",
            )

        decision = transition_phase(
            PhaseTransitionRequest(
                current_phase=session.current_phase,
                target_phase=payload.target_phase,
            )
        )
        if isinstance(decision, PhaseTransitionRejected):
            return self._reject(
                context,
                _PhaseRejectReason.INVALID_PHASE_TRANSITION,
            )

        if (
            validated_visibility.previous_phase is not decision.previous_phase
            or validated_visibility.target_phase is not decision.resulting_phase
        ):
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "CHANGE_PHASE_VISIBILITY_BINDING_MISMATCH",
            )

        ownership = context.ownership_evidence
        if not isinstance(ownership, ControlOwnershipBuildEvidence):
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "CHANGE_PHASE_OWNERSHIP_INVALID",
            )
        try:
            validated_ownership = _reconstruct(
                ownership,
                ControlOwnershipBuildEvidence,
            )
        except (
            AttributeError,
            ControlApplyBuildContextError,
            TypeError,
            ValueError,
        ):
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "CHANGE_PHASE_OWNERSHIP_INVALID",
            )
        if (
            validated_ownership.game_id,
            validated_ownership.session_id,
            validated_ownership.group_id,
        ) != (session.game_id, session.session_id, session.group_id) or (
            validated_ownership.observed_state_version
            != session.state_version
        ):
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "CHANGE_PHASE_OWNERSHIP_BINDING_MISMATCH",
            )
        generation = validated_ownership.active_generation
        if generation is None:
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "CHANGE_PHASE_OWNERSHIP_MISSING",
            )
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation <= 0
        ):
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "CHANGE_PHASE_OWNERSHIP_INVALID",
            )

        try:
            candidate = CandidateGameSnapshot(
                game_id=current.game_id,
                session_id=current.session_id,
                group_id=current.group_id,
                dm_participant_id=current.dm_participant_id,
                status=current.status,
                current_phase=decision.resulting_phase,
                state_version=current.state_version + 1,
                last_applied_sequence_no=context.envelope.event_sequence_no,
                snapshot_schema_version=current.snapshot_schema_version,
                lifecycle=current.lifecycle,
                phase=PhaseSnapshotSlice(
                    schema_version=current.phase.schema_version,
                    domain_version=current.phase.domain_version + 1,
                    phase=decision.resulting_phase,
                ),
                setup=current.setup,
                participants=current.participants,
                game_rules=current.game_rules,
                quest=current.quest,
                hidden_state=current.hidden_state,
            )
        except (CompositeSnapshotContractError, TypeError, ValueError):
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "COMPOSITE_CANDIDATE_INVALID",
            )

        event = self._result_event(
            context,
            event_type=GameEventType.PHASE_CHANGED,
            ordinal=1,
            payload=PhaseChangedPayload(
                command_id=context.envelope.command_id,
                operation_id=context.envelope.operation_id,
                input_event_id=context.envelope.event.event_id,
                result_code="CHANGE_PHASE_APPLIED",
                result_state_version=candidate.state_version,
                previous_phase=decision.previous_phase,
                current_phase=decision.resulting_phase,
            ),
        )
        ownership_intent = OwnershipIntent(
            intent_type=OwnershipIntentType.RETAIN,
            expected_generation=generation,
            resulting_generation=generation,
        )
        try:
            plan = ControlApplyPlan(
                game_id=session.game_id,
                session_id=session.session_id,
                group_id=session.group_id,
                command_id=context.envelope.command_id,
                command_type=SessionCommandType.CHANGE_PHASE,
                operation_id=context.envelope.operation_id,
                operation_claim_id=context.claim.claim_id,
                input_event_id=context.envelope.event.event_id,
                input_sequence_no=context.envelope.event_sequence_no,
                expected_state_version=current_version,
                expected_cursor=session.last_applied_sequence_no,
                expected_binding_version=context.envelope.requester_binding_version,
                candidate_snapshot=candidate,
                participant_mutations=(),
                setup_mutations=(),
                ownership_intent=ownership_intent,
                result_events=(event,),
                operation_terminal_state=ControlOperationStatus.SUCCESS,
                lifecycle_evidence=context.lifecycle_evidence,
                setup_participant_evidence=context.setup_participant_evidence,
            )
        except (TypeError, ValueError):
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "PHASE_CONTROL_PLAN_INVALID",
            )
        return BuildPlanReady(plan=plan)

    def _reject(
        self,
        context: ControlApplyBuildContext,
        reason: _PhaseRejectReason,
    ) -> BuildReject:
        session = context.session_view
        envelope = context.envelope
        event = self._result_event(
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
                command_type=SessionCommandType.CHANGE_PHASE,
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

    @staticmethod
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


def _revalidate_consumed_context(
    context: ControlApplyBuildContext,
) -> bool:
    """Re-run constructors for nested values consumed by this builder."""

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
            or not isinstance(context.participant_views, (tuple, list))
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

        intent_schema_version = context.command_intent.intent_schema_version
        command_type = context.command_intent.command_type
        command_payload = context.command_intent.payload
        target_phase = (
            command_payload.target_phase
            if isinstance(command_payload, ChangePhasePayload)
            else None
        )
        event = _reconstruct(context.envelope.event, GameEvent)
        envelope = _reconstruct(
            context.envelope,
            ControlEventDeliveryEnvelope,
            event=event,
        )
        claim = _reconstruct(context.claim, ControlOperationClaim)
        session = _reconstruct(
            context.session_view,
            ControlSessionBuildView,
            current_game_snapshot=None,
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
        seed = _reconstruct(context.result_event_seed, ControlResultEventSeed)

        if (
            command_type is SessionCommandType.CHANGE_PHASE
            and type(command_payload) is ChangePhasePayload
            and isinstance(target_phase, GamePhase)
        ):
            payload = ChangePhasePayload(target_phase=target_phase)
            intent = _reconstruct(
                context.command_intent,
                CanonicalControlCommandIntent,
                intent_schema_version=intent_schema_version,
                payload=payload,
            )
            ControlApplyBuildContext(
                envelope=envelope,
                claim=claim,
                command_intent=intent,
                session_view=session,
                participant_views=participants,
                setup_view=setup,
                ownership_evidence=context.ownership_evidence,
                result_event_seed=seed,
                lifecycle_evidence=context.lifecycle_evidence,
                setup_participant_evidence=context.setup_participant_evidence,
            )
    except (
        AttributeError,
        ControlApplyBuildContextError,
        TypeError,
        ValueError,
    ):
        return False
    return True


def _invalid_consumed_context_outcome(
    context: ControlApplyBuildContext,
) -> BuildNonCommit:
    """Preserve closed error categories without reading command internals."""

    try:
        if not isinstance(
            context.lifecycle_evidence,
            ControlLifecycleApplyEvidence,
        ):
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "CHANGE_PHASE_EVIDENCE_INCOMPLETE",
            )
        if not isinstance(
            context.ownership_evidence,
            ControlOwnershipBuildEvidence,
        ):
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "CHANGE_PHASE_OWNERSHIP_INVALID",
            )
    except AttributeError:
        pass
    return _noncommit(
        BuildNonCommitReason.INVALID_CONTEXT,
        "CONTROL_APPLY_CONTEXT_INVALID",
    )


def _revalidate_candidate_snapshot(
    current: CandidateGameSnapshot,
) -> CandidateGameSnapshot:
    """Re-run the complete composite and nested slice contract tree."""

    lifecycle = _reconstruct(current.lifecycle, LifecycleSnapshotSlice)
    phase = _reconstruct(current.phase, PhaseSnapshotSlice)
    setup = _reconstruct(current.setup, SetupSnapshotSlice)

    original_records = current.participants.participants
    if not isinstance(original_records, tuple):
        raise TypeError("snapshot participants must be a tuple")
    records = tuple(
        _reconstruct(record, ParticipantSnapshotRecord)
        for record in original_records
    )
    participants = _reconstruct(
        current.participants,
        ParticipantSnapshotSlice,
        participants=records,
    )
    game_rules = _reconstruct(current.game_rules, GameRuleSnapshotSlice)
    quest = _reconstruct(current.quest, QuestSnapshotSlice)
    hidden_state = _reconstruct(current.hidden_state, HiddenGameStateSlice)
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
        lifecycle=lifecycle,
        phase=phase,
        setup=setup,
        participants=participants,
        game_rules=game_rules,
        quest=quest,
        hidden_state=hidden_state,
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


def _validate_composite_bindings(
    context: ControlApplyBuildContext,
    current: CandidateGameSnapshot,
) -> BuildNonCommit | None:
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
    if current.state_version != session.state_version:
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "COMPOSITE_STATE_VERSION_MISMATCH",
        )
    if (
        not isinstance(current.last_applied_sequence_no, int)
        or isinstance(current.last_applied_sequence_no, bool)
        or current.last_applied_sequence_no < 0
        or current.last_applied_sequence_no > session.last_applied_sequence_no
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
                key=lambda view: view.participant_id,
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
            view is not None
            and view.script_id == setup.script_id
            and view.public_name == setup.public_name
            and view.manifest_reference == setup.manifest_reference
            and view.setup_version == setup.domain_version
        )
    if not setup_matches:
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "COMPOSITE_SETUP_BINDING_MISMATCH",
        )
    return None


def _noncommit(
    reason: BuildNonCommitReason,
    detail_code: str,
) -> BuildNonCommit:
    return BuildNonCommit(reason=reason, detail_code=detail_code)
