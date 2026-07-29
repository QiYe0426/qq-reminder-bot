from __future__ import annotations

from datetime import datetime, timezone

import pytest

from game_runtime.event import (
    EventVisibility,
    GameEvent,
    GameEventSource,
    GameEventType,
    PhaseChangedPayload,
    SessionControlRejectedPayload,
    SessionEndedPayload,
    SessionPausedPayload,
    SessionResumedPayload,
    SessionStartedPayload,
    validate_control_result_event,
)
from game_runtime.participant import ParticipantMembershipState, ParticipantType
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control import (
    BuildNonCommit,
    BuildNonCommitReason,
    BuildPlanReady,
    BuildReject,
    CanonicalControlCommandIntent,
    ChangePhasePayload,
    ControlApplyBuildContext,
    ControlEndRetentionEvidence,
    ControlEventDeliveryEnvelope,
    ControlLifecycleApplyEvidence,
    ControlOperationClaim,
    ControlOwnershipBuildEvidence,
    ControlParticipantBuildView,
    ControlPhaseVisibilityIntent,
    ControlResultEventSeed,
    ControlResumeValidationEvidence,
    ControlSessionBuildView,
    ControlSetupBuildView,
    ControlStartReadinessEvidence,
    DMCommandEventPayload,
    EndGamePayload,
    HunterInstanceReadinessStatus,
    LifecyclePhaseControlApplyPlanBuilder,
    LifecyclePhaseRejectReason,
    OwnershipIntentType,
    PauseGamePayload,
    PhaseVisibilityDecision,
    ResumeGamePayload,
    ResumeValidationStatus,
    SessionCommandType,
    StartGamePayload,
    StartReadinessStatus,
    fingerprint_payload,
)


NOW = datetime(2026, 7, 17, 9, 30, tzinfo=timezone.utc)
_DEFAULT_EVIDENCE = object()


def _start_evidence(state_version: int) -> ControlStartReadinessEvidence:
    return ControlStartReadinessEvidence(
        contract_version=1,
        game_id="game-1",
        session_id="session-1",
        observed_state_version=state_version,
        readiness_status=StartReadinessStatus.READY,
        readiness_evidence_reference="readiness-1",
        setup_manifest_reference="manifest-1",
        setup_manifest_version=1,
        participant_roster_reference="roster-1",
        participant_roster_version=2,
        character_assignment_set_reference="assignments-1",
        character_assignment_version=3,
        hunter_instance_reference="hunter-1",
        hunter_instance_version=4,
        hunter_instance_status=HunterInstanceReadinessStatus.READY,
        policy_reference="policy-1",
        policy_version=5,
        template_reference="template-1",
        template_version=6,
        knowledge_partition_reference="knowledge-1",
        knowledge_partition_version=7,
    )


def _resume_evidence(state_version: int) -> ControlResumeValidationEvidence:
    return ControlResumeValidationEvidence(
        contract_version=1,
        game_id="game-1",
        session_id="session-1",
        observed_state_version=state_version,
        validation_status=ResumeValidationStatus.READY,
        recovery_validation_reference="recovery-validation-1",
        validated_snapshot_reference=f"snapshot-{state_version}",
        validated_state_version=state_version,
        validated_cursor=6,
        previous_pause_event_reference="event-pause-6",
        previous_pause_state_version=state_version,
        previous_pause_sequence_no=6,
        ownership_evidence_reference="ownership-3",
        ownership_generation=3,
    )


def _end_evidence(state_version: int) -> ControlEndRetentionEvidence:
    return ControlEndRetentionEvidence(
        contract_version=1,
        game_id="game-1",
        session_id="session-1",
        observed_state_version=state_version,
        retention_policy_reference="retention-policy-1",
        retention_policy_version=1,
        retention_reference="retention-1",
        retention_timestamp=NOW,
        public_result_reference="public-result-1",
    )


def _phase_evidence(
    state_version: int,
    previous_phase: GamePhase,
    target_phase: GamePhase,
) -> ControlPhaseVisibilityIntent:
    return ControlPhaseVisibilityIntent(
        contract_version=1,
        game_id="game-1",
        session_id="session-1",
        expected_state_version=state_version,
        previous_phase=previous_phase,
        target_phase=target_phase,
        visibility_policy_reference="visibility-policy-1",
        visibility_policy_version=1,
        decision=PhaseVisibilityDecision.CHANGE_SET,
        change_set_reference="visibility-change-1",
        intent_reference="visibility-intent-1",
    )


def _command_payload(
    command_type: SessionCommandType,
    *,
    target_phase: GamePhase,
):
    if command_type is SessionCommandType.START_GAME:
        return StartGamePayload()
    if command_type is SessionCommandType.PAUSE_GAME:
        return PauseGamePayload(reason_code="DM_REQUEST")
    if command_type is SessionCommandType.RESUME_GAME:
        return ResumeGamePayload()
    if command_type is SessionCommandType.END_GAME:
        return EndGamePayload(public_result_reference="public-result-1")
    if command_type is SessionCommandType.CHANGE_PHASE:
        return ChangePhasePayload(target_phase=target_phase)
    raise AssertionError(f"unsupported test command: {command_type.value}")


def _default_lifecycle_evidence(
    command_type: SessionCommandType,
    *,
    state_version: int,
    current_phase: GamePhase,
    target_phase: GamePhase,
) -> ControlLifecycleApplyEvidence:
    if command_type is SessionCommandType.START_GAME:
        return ControlLifecycleApplyEvidence(
            start_readiness=_start_evidence(state_version),
            phase_visibility_intent=_phase_evidence(
                state_version,
                GamePhase.LOBBY,
                GamePhase.INTRODUCTION,
            ),
        )
    if command_type is SessionCommandType.RESUME_GAME:
        return ControlLifecycleApplyEvidence(
            resume_validation=_resume_evidence(state_version)
        )
    if command_type is SessionCommandType.END_GAME:
        return ControlLifecycleApplyEvidence(
            end_retention=_end_evidence(state_version)
        )
    if command_type is SessionCommandType.CHANGE_PHASE:
        return ControlLifecycleApplyEvidence(
            phase_visibility_intent=_phase_evidence(
                state_version,
                current_phase,
                target_phase,
            )
        )
    return ControlLifecycleApplyEvidence()


def make_context(
    command_type: SessionCommandType,
    *,
    status: GameSessionStatus,
    current_phase: GamePhase,
    target_phase: GamePhase = GamePhase.DISCUSSION,
    session_version: int = 4,
    command_observed_version: int | None = None,
    active_generation: int | None | object = object(),
    lifecycle_evidence: ControlLifecycleApplyEvidence | object = _DEFAULT_EVIDENCE,
) -> ControlApplyBuildContext:
    if command_observed_version is None:
        command_observed_version = session_version
    payload = _command_payload(command_type, target_phase=target_phase)
    payload_fingerprint = fingerprint_payload(payload)
    event_payload = DMCommandEventPayload(
        command_id="command-1",
        command_type=command_type,
        requester="participant-dm",
        causation_event_id="request-event-1",
        observed_state_version=command_observed_version,
        payload_reference=f"sha256:{payload_fingerprint}",
        payload_fingerprint=payload_fingerprint,
    )
    event = GameEvent(
        event_id="event-7",
        game_id="game-1",
        session_id="session-1",
        event_type=GameEventType.DM_COMMAND,
        actor="participant-dm",
        source=GameEventSource.CONTROL,
        correlation_id="correlation-1",
        timestamp=NOW,
        payload=event_payload.to_mapping(),
        visibility=EventVisibility.DM_CONTROL,
        observed_state_version=command_observed_version,
        causation_event_id="request-event-1",
    )
    envelope = ControlEventDeliveryEnvelope(
        event=event,
        event_sequence_no=7,
        operation_id="operation-1",
        command_id="command-1",
        observed_state_version=command_observed_version,
        requester_principal_ref="participant-dm",
        requester_binding_version=2,
        authorization_reference="authorization-1",
        confirmation_reference="confirmation-1",
        correlation_id="correlation-1",
        stored_event_reference="event-7",
    )
    claim = ControlOperationClaim(
        game_id="game-1",
        session_id="session-1",
        command_id="command-1",
        operation_id="operation-1",
        input_event_id="event-7",
        claim_id="claim-1",
        claimed_at=NOW,
    )
    intent = CanonicalControlCommandIntent(
        intent_schema_version=1,
        command_type=command_type,
        payload=payload,
        payload_fingerprint=payload_fingerprint,
    )
    session_view = ControlSessionBuildView(
        game_id="game-1",
        session_id="session-1",
        group_id="group-1",
        dm_participant_id="participant-dm",
        status=status,
        current_phase=current_phase,
        state_version=session_version,
        last_applied_sequence_no=6,
    )
    participant_view = ControlParticipantBuildView(
        game_id="game-1",
        session_id="session-1",
        participant_id="participant-dm",
        participant_type=ParticipantType.DM,
        membership_state=ParticipantMembershipState.ACTIVE,
        character_id=None,
        binding_version=2,
    )
    setup_view = ControlSetupBuildView(
        game_id="game-1",
        session_id="session-1",
        script_id="script-1",
        public_name="Public Script",
        manifest_reference="manifest-1",
        setup_version=1,
    )
    if not isinstance(active_generation, int) and active_generation is not None:
        active_generation = (
            None
            if command_type is SessionCommandType.START_GAME
            or (
                command_type is SessionCommandType.END_GAME
                and status is GameSessionStatus.CREATED
            )
            else 3
        )
    ownership = ControlOwnershipBuildEvidence(
        game_id="game-1",
        session_id="session-1",
        group_id="group-1",
        active_generation=active_generation,
        last_allocated_generation=3,
        observed_state_version=session_version,
    )
    seed = ControlResultEventSeed(
        seed_contract_version=1,
        game_id="game-1",
        session_id="session-1",
        command_id="command-1",
        operation_id="operation-1",
        input_event_id="event-7",
        timestamp=NOW,
        correlation_id="correlation-1",
    )
    if lifecycle_evidence is _DEFAULT_EVIDENCE:
        lifecycle_evidence = _default_lifecycle_evidence(
            command_type,
            state_version=session_version,
            current_phase=current_phase,
            target_phase=target_phase,
        )
    assert isinstance(lifecycle_evidence, ControlLifecycleApplyEvidence)
    return ControlApplyBuildContext(
        envelope=envelope,
        claim=claim,
        command_intent=intent,
        session_view=session_view,
        participant_views=(participant_view,),
        setup_view=setup_view,
        ownership_evidence=ownership,
        result_event_seed=seed,
        lifecycle_evidence=lifecycle_evidence,
    )


def test_start_game_builds_candidate_ownership_and_two_result_events() -> None:
    context = make_context(
        SessionCommandType.START_GAME,
        status=GameSessionStatus.CREATED,
        current_phase=GamePhase.LOBBY,
    )

    outcome = LifecyclePhaseControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildPlanReady)
    plan = outcome.plan
    assert plan.candidate_snapshot.status is GameSessionStatus.RUNNING
    assert plan.candidate_snapshot.current_phase is GamePhase.INTRODUCTION
    assert plan.candidate_snapshot.state_version == 5
    assert plan.ownership_intent.intent_type is OwnershipIntentType.ACQUIRE
    assert plan.ownership_intent.resulting_generation == 4
    assert tuple(event.event_type for event in plan.result_events) == (
        GameEventType.SESSION_STARTED,
        GameEventType.PHASE_CHANGED,
    )
    started = validate_control_result_event(plan.result_events[0])
    changed = validate_control_result_event(plan.result_events[1])
    assert isinstance(started, SessionStartedPayload)
    assert started.ownership_generation == 4
    assert isinstance(changed, PhaseChangedPayload)
    assert changed.current_phase is GamePhase.INTRODUCTION
    assert plan.lifecycle_evidence == context.lifecycle_evidence


def test_pause_game_preserves_phase_and_ownership_generation() -> None:
    context = make_context(
        SessionCommandType.PAUSE_GAME,
        status=GameSessionStatus.RUNNING,
        current_phase=GamePhase.EXPLORATION,
    )

    outcome = LifecyclePhaseControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildPlanReady)
    assert outcome.plan.candidate_snapshot.status is GameSessionStatus.PAUSED
    assert outcome.plan.candidate_snapshot.current_phase is GamePhase.EXPLORATION
    assert outcome.plan.ownership_intent.intent_type is OwnershipIntentType.RETAIN
    assert outcome.plan.ownership_intent.resulting_generation == 3
    payload = validate_control_result_event(outcome.plan.result_events[0])
    assert isinstance(payload, SessionPausedPayload)
    assert payload.reason_code == "DM_REQUEST"


def test_resume_game_retains_existing_ownership_generation() -> None:
    context = make_context(
        SessionCommandType.RESUME_GAME,
        status=GameSessionStatus.PAUSED,
        current_phase=GamePhase.DISCUSSION,
    )

    outcome = LifecyclePhaseControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildPlanReady)
    assert outcome.plan.candidate_snapshot.status is GameSessionStatus.RUNNING
    assert outcome.plan.candidate_snapshot.current_phase is GamePhase.DISCUSSION
    assert outcome.plan.ownership_intent.intent_type is OwnershipIntentType.RETAIN
    assert outcome.plan.ownership_intent.resulting_generation == 3
    payload = validate_control_result_event(outcome.plan.result_events[0])
    assert isinstance(payload, SessionResumedPayload)
    assert payload.ownership_generation == 3


@pytest.mark.parametrize(
    ("status", "phase", "intent_type"),
    [
        (GameSessionStatus.CREATED, GamePhase.LOBBY, OwnershipIntentType.UNCHANGED),
        (GameSessionStatus.RUNNING, GamePhase.EXPLORATION, OwnershipIntentType.RELEASE),
        (GameSessionStatus.PAUSED, GamePhase.DISCUSSION, OwnershipIntentType.RELEASE),
    ],
)
def test_end_game_supports_frozen_states_and_retention(
    status: GameSessionStatus,
    phase: GamePhase,
    intent_type: OwnershipIntentType,
) -> None:
    context = make_context(
        SessionCommandType.END_GAME,
        status=status,
        current_phase=phase,
    )

    outcome = LifecyclePhaseControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildPlanReady)
    assert outcome.plan.candidate_snapshot.status is GameSessionStatus.ENDED
    assert outcome.plan.candidate_snapshot.current_phase is GamePhase.ENDING
    assert outcome.plan.ownership_intent.intent_type is intent_type
    payload = validate_control_result_event(outcome.plan.result_events[0])
    assert isinstance(payload, SessionEndedPayload)
    assert payload.retention_reference == "retention-1"


@pytest.mark.parametrize(
    ("previous_phase", "target_phase"),
    [
        (GamePhase.INTRODUCTION, GamePhase.EXPLORATION),
        (GamePhase.EXPLORATION, GamePhase.DISCUSSION),
        (GamePhase.DISCUSSION, GamePhase.EXPLORATION),
        (GamePhase.DISCUSSION, GamePhase.VOTING),
        (GamePhase.VOTING, GamePhase.DISCUSSION),
        (GamePhase.VOTING, GamePhase.ENDING),
    ],
)
def test_change_phase_supports_frozen_graph_and_visibility_intent(
    previous_phase: GamePhase,
    target_phase: GamePhase,
) -> None:
    context = make_context(
        SessionCommandType.CHANGE_PHASE,
        status=GameSessionStatus.RUNNING,
        current_phase=previous_phase,
        target_phase=target_phase,
    )

    outcome = LifecyclePhaseControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildPlanReady)
    assert outcome.plan.candidate_snapshot.current_phase is target_phase
    assert outcome.plan.ownership_intent.intent_type is OwnershipIntentType.RETAIN
    assert outcome.plan.lifecycle_evidence.phase_visibility_intent == (
        context.lifecycle_evidence.phase_visibility_intent
    )
    payload = validate_control_result_event(outcome.plan.result_events[0])
    assert isinstance(payload, PhaseChangedPayload)
    assert payload.previous_phase is previous_phase
    assert payload.current_phase is target_phase


def test_invalid_lifecycle_transition_builds_deterministic_reject() -> None:
    context = make_context(
        SessionCommandType.PAUSE_GAME,
        status=GameSessionStatus.PAUSED,
        current_phase=GamePhase.EXPLORATION,
    )

    outcome = LifecyclePhaseControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildReject)
    payload = validate_control_result_event(outcome.plan.rejection_event)
    assert isinstance(payload, SessionControlRejectedPayload)
    assert payload.reason_code == (
        LifecyclePhaseRejectReason.INVALID_LIFECYCLE_TRANSITION.value
    )


def test_invalid_phase_transition_builds_deterministic_reject() -> None:
    context = make_context(
        SessionCommandType.CHANGE_PHASE,
        status=GameSessionStatus.RUNNING,
        current_phase=GamePhase.EXPLORATION,
        target_phase=GamePhase.VOTING,
    )

    outcome = LifecyclePhaseControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildReject)
    payload = validate_control_result_event(outcome.plan.rejection_event)
    assert isinstance(payload, SessionControlRejectedPayload)
    assert payload.reason_code == (
        LifecyclePhaseRejectReason.INVALID_PHASE_TRANSITION.value
    )


def test_stale_version_builds_reject_against_current_actor_version() -> None:
    context = make_context(
        SessionCommandType.PAUSE_GAME,
        status=GameSessionStatus.RUNNING,
        current_phase=GamePhase.EXPLORATION,
        session_version=5,
        command_observed_version=4,
    )

    outcome = LifecyclePhaseControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildReject)
    assert outcome.plan.expected_state_version == 5
    assert outcome.plan.rejection_event.observed_state_version == 5
    payload = validate_control_result_event(outcome.plan.rejection_event)
    assert isinstance(payload, SessionControlRejectedPayload)
    assert payload.state_version == 5
    assert payload.reason_code == LifecyclePhaseRejectReason.STALE_STATE_VERSION.value


def test_missing_required_evidence_returns_noncommit_without_event() -> None:
    context = make_context(
        SessionCommandType.START_GAME,
        status=GameSessionStatus.CREATED,
        current_phase=GamePhase.LOBBY,
        lifecycle_evidence=ControlLifecycleApplyEvidence(),
    )

    outcome = LifecyclePhaseControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildNonCommit)
    assert outcome.reason is BuildNonCommitReason.EVIDENCE_MISMATCH
    assert not hasattr(outcome, "plan")


def test_result_event_identity_is_deterministic_for_same_context() -> None:
    context = make_context(
        SessionCommandType.START_GAME,
        status=GameSessionStatus.CREATED,
        current_phase=GamePhase.LOBBY,
    )
    builder = LifecyclePhaseControlApplyPlanBuilder()

    first = builder.build(context)
    second = builder.build(context)

    assert isinstance(first, BuildPlanReady)
    assert isinstance(second, BuildPlanReady)
    assert tuple(event.event_id for event in first.plan.result_events) == tuple(
        event.event_id for event in second.plan.result_events
    )
    assert first.plan.result_events[0].event_id == (
        context.result_event_seed.derive_event_id(
            GameEventType.SESSION_STARTED,
            ordinal=1,
        )
    )
    assert first.plan.result_events[1].event_id == (
        context.result_event_seed.derive_event_id(
            GameEventType.PHASE_CHANGED,
            ordinal=2,
        )
    )
