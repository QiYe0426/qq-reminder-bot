"""Pure composite-native ApplyPlan builder for SET_SCRIPT."""

from __future__ import annotations

from dataclasses import fields

from game_runtime.event import (
    GameEvent,
    GameEventSource,
    GameEventType,
    ScriptSetPayload,
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
    SessionCommandType,
    SetScriptPayload,
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
    SetupSnapshotSlice,
)
from game_runtime.session_control.delivery import ControlEventDeliveryEnvelope
from game_runtime.session_control.operation import ControlOperationStatus
from game_runtime.session_control.setup_participant_builder import (
    SetupParticipantRejectReason,
)
from game_runtime.session_control.setup_participant_evidence import (
    ControlScriptApplyEvidence,
    ControlSetupParticipantApplyEvidence,
    ControlSetupParticipantEvidenceError,
)
from game_runtime.session_control.setup_transition import (
    SetupTransitionAccepted,
    SetupTransitionContractError,
    SetupTransitionRejected,
    SetupTransitionRejectReason,
    SetupTransitionRequest,
    SetupTransitionState,
    transition_setup,
)


class SetupControlApplyPlanBuilder:
    """Build one deterministic composite SET_SCRIPT plan."""

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
        if intent.command_type is not SessionCommandType.SET_SCRIPT:
            command_type = intent.command_type
            detail = (
                f"{command_type.value}_REDUCER_UNAVAILABLE"
                if isinstance(command_type, SessionCommandType)
                else "CONTROL_COMMAND_TYPE_INVALID"
            )
            return _noncommit(
                (
                    BuildNonCommitReason.REDUCER_UNAVAILABLE
                    if isinstance(command_type, SessionCommandType)
                    else BuildNonCommitReason.INVALID_CONTEXT
                ),
                detail,
            )
        payload = intent.payload
        if type(payload) is not SetScriptPayload:
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "SET_SCRIPT_INPUT_INVALID",
            )

        session = context.session_view
        observed_version = context.envelope.observed_state_version
        if session.state_version != observed_version:
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "CONTROL_STATE_VERSION_MISMATCH",
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

        if session.status is GameSessionStatus.PAUSED:
            return _noncommit(
                BuildNonCommitReason.RECOVERY_REQUIRED,
                "SET_SCRIPT_REPAIR_EVIDENCE_REQUIRED",
            )
        if (
            session.status is not GameSessionStatus.CREATED
            or session.current_phase is not GamePhase.LOBBY
        ):
            return self._reject(
                context,
                SetupParticipantRejectReason.INVALID_LIFECYCLE,
            )

        bundle = context.setup_participant_evidence
        try:
            bundle.validate_for_command(
                SessionCommandType.SET_SCRIPT,
                game_id=session.game_id,
                session_id=session.session_id,
                observed_state_version=session.state_version,
            )
        except (
            AttributeError,
            ControlSetupParticipantEvidenceError,
            TypeError,
            ValueError,
        ):
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "SET_SCRIPT_EVIDENCE_INCOMPLETE",
            )
        evidence = bundle.script_apply
        if not isinstance(evidence, ControlScriptApplyEvidence):
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "SET_SCRIPT_EVIDENCE_INCOMPLETE",
            )
        try:
            evidence = _reconstruct(
                evidence,
                ControlScriptApplyEvidence,
            )
        except (
            AttributeError,
            ControlSetupParticipantEvidenceError,
            TypeError,
            ValueError,
        ):
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "SET_SCRIPT_EVIDENCE_INCOMPLETE",
            )

        ownership_failure = _validate_ownership(context, evidence)
        if ownership_failure is not None:
            return ownership_failure
        setup = current.setup
        if (
            evidence.script_id != payload.script_id
            or evidence.manifest_reference != payload.manifest_reference
            or evidence.expected_setup_version != setup.domain_version
            or evidence.resulting_setup_version != setup.domain_version + 1
        ):
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "SET_SCRIPT_EVIDENCE_MISMATCH",
            )
        if (
            setup.manifest_reference == payload.manifest_reference
            and setup.manifest_version is not None
            and evidence.manifest_version != setup.manifest_version
        ):
            return _noncommit(
                BuildNonCommitReason.RECOVERY_REQUIRED,
                "SET_SCRIPT_MANIFEST_VERSION_MISMATCH",
            )

        try:
            decision = transition_setup(
                SetupTransitionRequest(
                    current_state=SetupTransitionState(
                        script_id=setup.script_id,
                        manifest_reference=setup.manifest_reference,
                    ),
                    requested_state=SetupTransitionState(
                        script_id=payload.script_id,
                        manifest_reference=payload.manifest_reference,
                    ),
                )
            )
        except (SetupTransitionContractError, TypeError, ValueError):
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "SET_SCRIPT_TRANSITION_CONTEXT_INVALID",
            )
        if isinstance(decision, SetupTransitionRejected):
            if (
                decision.reason
                is SetupTransitionRejectReason.SCRIPT_ALREADY_SET
            ):
                return self._reject(
                    context,
                    SetupParticipantRejectReason.SCRIPT_ALREADY_SET,
                )
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "SET_SCRIPT_TRANSITION_CONTEXT_INVALID",
            )
        if not isinstance(decision, SetupTransitionAccepted):
            return _noncommit(
                BuildNonCommitReason.INVALID_CONTEXT,
                "SET_SCRIPT_TRANSITION_CONTEXT_INVALID",
            )
        if (
            setup.domain_version > 0
            and context.envelope.confirmation_reference is None
        ):
            return self._reject(
                context,
                SetupParticipantRejectReason.SCRIPT_REPLACEMENT_NOT_CONFIRMED,
            )
        return _compose_setup_apply_plan(
            context=context,
            current=current,
            decision=decision,
            evidence=evidence,
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
                command_type=SessionCommandType.SET_SCRIPT,
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


def _compose_setup_apply_plan(
    *,
    context: ControlApplyBuildContext,
    current: CandidateGameSnapshot,
    decision: SetupTransitionAccepted,
    evidence: ControlScriptApplyEvidence,
) -> BuildPlanReady | BuildNonCommit:
    """Compose and validate one complete SET_SCRIPT ApplyPlan."""

    payload = context.command_intent.payload
    if (
        not isinstance(payload, SetScriptPayload)
        or not isinstance(current, CandidateGameSnapshot)
        or not isinstance(decision, SetupTransitionAccepted)
        or not isinstance(evidence, ControlScriptApplyEvidence)
        or decision.resulting_state.script_id != payload.script_id
        or (
            decision.resulting_state.manifest_reference
            != payload.manifest_reference
        )
    ):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "SET_SCRIPT_COMPOSITION_INVALID",
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
            setup=SetupSnapshotSlice(
                schema_version=current.setup.schema_version,
                domain_version=current.setup.domain_version + 1,
                script_id=decision.resulting_state.script_id,
                public_name=payload.public_name,
                manifest_reference=(
                    decision.resulting_state.manifest_reference
                ),
                manifest_version=evidence.manifest_version,
            ),
            participants=current.participants,
            game_rules=current.game_rules,
            hidden_state=current.hidden_state,
        )
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
                command_id=envelope.command_id,
                operation_id=envelope.operation_id,
                input_event_id=envelope.event.event_id,
                result_code="SET_SCRIPT_APPLIED",
                result_state_version=candidate.state_version,
                script_id=payload.script_id,
                public_name=payload.public_name,
                manifest_reference=payload.manifest_reference,
            ),
        )
        plan = ControlApplyPlan(
            game_id=session.game_id,
            session_id=session.session_id,
            group_id=session.group_id,
            command_id=envelope.command_id,
            command_type=SessionCommandType.SET_SCRIPT,
            operation_id=envelope.operation_id,
            operation_claim_id=context.claim.claim_id,
            input_event_id=envelope.event.event_id,
            input_sequence_no=envelope.event_sequence_no,
            expected_state_version=session.state_version,
            expected_cursor=session.last_applied_sequence_no,
            expected_binding_version=envelope.requester_binding_version,
            candidate_snapshot=candidate,
            participant_mutations=(),
            setup_mutations=(mutation,),
            ownership_intent=OwnershipIntent(
                intent_type=OwnershipIntentType.UNCHANGED,
                expected_generation=None,
                resulting_generation=None,
            ),
            result_events=(event,),
            operation_terminal_state=ControlOperationStatus.SUCCESS,
            lifecycle_evidence=context.lifecycle_evidence,
            setup_participant_evidence=context.setup_participant_evidence,
        )
    except (
        AttributeError,
        CompositeSnapshotContractError,
        ControlApplyBuildContextError,
        ControlSetupParticipantEvidenceError,
        TypeError,
        ValueError,
    ):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "SET_SCRIPT_COMPOSITION_INVALID",
        )
    if (
        plan.candidate_snapshot is not candidate
        or plan.setup_mutations != (mutation,)
        or plan.participant_mutations
        or plan.result_events != (event,)
        or plan.ownership_intent.intent_type
        is not OwnershipIntentType.UNCHANGED
    ):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "SET_SCRIPT_COMPOSITION_INVALID",
        )
    return BuildPlanReady(plan=plan)


def _validate_ownership(
    context: ControlApplyBuildContext,
    evidence: ControlScriptApplyEvidence,
) -> BuildNonCommit | None:
    ownership = context.ownership_evidence
    try:
        validated = _reconstruct(
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
            "SET_SCRIPT_OWNERSHIP_INVALID",
        )
    session = context.session_view
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
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "SET_SCRIPT_OWNERSHIP_BINDING_MISMATCH",
        )
    if (
        validated.active_generation is not None
        or evidence.ownership_generation is not None
    ):
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "SET_SCRIPT_OWNERSHIP_INVALID",
        )
    return None


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
    if current.state_version != session.state_version:
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "COMPOSITE_STATE_VERSION_MISMATCH",
        )
    if (
        current.last_applied_sequence_no
        > session.last_applied_sequence_no
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
        matches = (
            view is None
            and setup.script_id is None
            and setup.public_name is None
            and setup.manifest_reference is None
            and setup.manifest_version is None
        )
    else:
        matches = (
            isinstance(view, ControlSetupBuildView)
            and view.game_id == current.game_id
            and view.session_id == current.session_id
            and view.script_id == setup.script_id
            and view.public_name == setup.public_name
            and view.manifest_reference == setup.manifest_reference
            and view.setup_version == setup.domain_version
            and setup.manifest_version is not None
        )
    if not matches:
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "COMPOSITE_SETUP_BINDING_MISMATCH",
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
        if (
            context.command_intent.command_type
            is not SessionCommandType.SET_SCRIPT
            or type(payload) is not SetScriptPayload
        ):
            return True
        payload = SetScriptPayload(
            script_id=payload.script_id,
            public_name=payload.public_name,
            manifest_reference=payload.manifest_reference,
        )
        intent = _reconstruct(
            context.command_intent,
            CanonicalControlCommandIntent,
            payload=payload,
        )
        session = _reconstruct(
            context.session_view,
            ControlSessionBuildView,
            current_game_snapshot=context.session_view.current_game_snapshot,
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
        seed = _reconstruct(context.result_event_seed, ControlResultEventSeed)
        evidence = context.setup_participant_evidence
        script = evidence.script_apply
        if script is not None:
            script = _reconstruct(script, ControlScriptApplyEvidence)
        evidence = ControlSetupParticipantApplyEvidence(
            script_apply=script,
            character_assignment=evidence.character_assignment,
            player_replacement=evidence.player_replacement,
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
            lifecycle_evidence=context.lifecycle_evidence,
            setup_participant_evidence=evidence,
        )
    except (
        AttributeError,
        ControlApplyBuildContextError,
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
        evidence = context.setup_participant_evidence
        if (
            not isinstance(evidence, ControlSetupParticipantApplyEvidence)
            or not isinstance(evidence.script_apply, ControlScriptApplyEvidence)
        ):
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "SET_SCRIPT_EVIDENCE_INCOMPLETE",
            )
        ownership = context.ownership_evidence
        if not isinstance(ownership, ControlOwnershipBuildEvidence):
            return _noncommit(
                BuildNonCommitReason.EVIDENCE_MISMATCH,
                "SET_SCRIPT_OWNERSHIP_INVALID",
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
    lifecycle = _reconstruct(current.lifecycle, LifecycleSnapshotSlice)
    phase = _reconstruct(current.phase, PhaseSnapshotSlice)
    setup = _reconstruct(current.setup, SetupSnapshotSlice)
    original_records = current.participants.participants
    if not isinstance(original_records, tuple):
        raise TypeError("snapshot participants must be a tuple")
    participants = _reconstruct(
        current.participants,
        ParticipantSnapshotSlice,
        participants=tuple(
            _reconstruct(record, ParticipantSnapshotRecord)
            for record in original_records
        ),
    )
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
        game_rules=_reconstruct(
            current.game_rules,
            GameRuleSnapshotSlice,
        ),
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


def _noncommit(
    reason: BuildNonCommitReason,
    detail_code: str,
) -> BuildNonCommit:
    return BuildNonCommit(reason=reason, detail_code=detail_code)
