from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timezone
import importlib.util
import ast
import inspect

import pytest

import game_runtime.session_control.apply_contract as apply_contract
import game_runtime.session_control as session_control
from game_runtime.event import (
    EventVisibility,
    GameEvent,
    GameEventSource,
    GameEventType,
    RuleSetActivatedPayload,
    SessionControlRejectedPayload,
    validate_control_result_event,
)
from game_runtime.participant import ParticipantMembershipState, ParticipantType
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control import (
    CandidateGameSnapshot,
    CanonicalControlCommandIntent,
    ControlApplyBuildContext,
    ControlApplyPlan,
    ControlGameRuleApplyEvidence,
    ControlLifecycleApplyEvidence,
    ControlOperationClaim,
    ControlOperationStatus,
    ControlOwnershipBuildEvidence,
    ControlParticipantBuildView,
    ControlResultEventSeed,
    ControlRuleSetActivationEvidence,
    ControlSessionBuildView,
    ControlSetupBuildView,
    ControlPhaseVisibilityIntent,
    GameRuleEvidenceStatus,
    GameRuleSnapshotSlice,
    HiddenGameStateSlice,
    LifecycleSnapshotSlice,
    OwnershipIntent,
    OwnershipIntentType,
    ParticipantSnapshotRecord,
    ParticipantSnapshotSlice,
    PhaseSnapshotSlice,
    QuestSnapshotSlice,
    PhaseVisibilityDecision,
    SessionCommandType,
    SetupSnapshotSlice,
    ActivateRuleSetPayload,
    BuildNonCommit,
    BuildNonCommitReason,
    BuildPlanReady,
    BuildReject,
    CandidateSessionSnapshot,
)
from game_runtime.session_control.confirmation import fingerprint_payload
from game_runtime.session_control.delivery import ControlEventDeliveryEnvelope
from game_runtime.session_control.event_integration import DMCommandEventPayload


NOW = datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc)


def test_game_rule_mutation_is_closed_frozen_and_validates_single_version_advances() -> None:
    mutation_type = getattr(apply_contract, "GameRuleMutationType", None)
    mutation_contract = getattr(apply_contract, "GameRuleMutation", None)

    assert mutation_type is not None
    assert mutation_contract is not None
    assert tuple(member.name for member in mutation_type) == (
        "ACTIVATE_RULE_SET",
        "REVEAL_CLUE",
    )
    assert tuple(field.name for field in fields(mutation_contract)) == (
        "mutation_type",
        "manifest_reference",
        "committed_rule_set_reference",
        "committed_disclosure_state_reference",
        "opaque_hidden_state_reference",
        "expected_game_rule_version",
        "resulting_game_rule_version",
        "expected_hidden_state_version",
        "resulting_hidden_state_version",
        "provenance_reference",
    )

    mutation = mutation_contract(
        mutation_type=mutation_type.ACTIVATE_RULE_SET,
        manifest_reference="manifest-1",
        committed_rule_set_reference="rule-set:commit-1",
        committed_disclosure_state_reference="disclosure-state:commit-1",
        opaque_hidden_state_reference="hidden-state:commit-1",
        expected_game_rule_version=0,
        resulting_game_rule_version=1,
        expected_hidden_state_version=0,
        resulting_hidden_state_version=1,
        provenance_reference="provenance-1",
    )
    assert not hasattr(mutation, "__dict__")
    with pytest.raises(FrozenInstanceError):
        mutation.resulting_game_rule_version = 2
    with pytest.raises(ValueError, match="advance once"):
        mutation_contract(
            mutation_type=mutation_type.ACTIVATE_RULE_SET,
            manifest_reference="manifest-1",
            committed_rule_set_reference="rule-set:commit-1",
            committed_disclosure_state_reference="disclosure-state:commit-1",
            opaque_hidden_state_reference="hidden-state:commit-1",
            expected_game_rule_version=0,
            resulting_game_rule_version=2,
            expected_hidden_state_version=0,
            resulting_hidden_state_version=1,
            provenance_reference="provenance-1",
        )


def _activation_evidence(
    **overrides: object,
) -> ControlRuleSetActivationEvidence:
    values: dict[str, object] = {
        "evidence_schema_version": 1,
        "game_id": "game-1",
        "session_id": "session-1",
        "observed_state_version": 4,
        "setup_manifest_reference": "manifest-1",
        "setup_version": 2,
        "committed_rule_set_reference": "rule-set:commit-1",
        "initial_disclosure_state_reference": "disclosure-state:commit-1",
        "rule_set_version": 0,
        "opaque_hidden_state_reference": "hidden-state:commit-1",
        "hidden_state_version": 0,
        "provenance_reference": "provenance-1",
        "validation_status": GameRuleEvidenceStatus.VERIFIED,
    }
    values.update(overrides)
    return ControlRuleSetActivationEvidence(**values)  # type: ignore[arg-type]


def _candidate() -> CandidateGameSnapshot:
    return CandidateGameSnapshot(
        game_id="game-1",
        session_id="session-1",
        group_id="group-1",
        dm_participant_id="dm-1",
        status=GameSessionStatus.CREATED,
        current_phase=GamePhase.LOBBY,
        state_version=5,
        last_applied_sequence_no=7,
        snapshot_schema_version=3,
        lifecycle=LifecycleSnapshotSlice(
            schema_version=1,
            domain_version=1,
            status=GameSessionStatus.CREATED,
        ),
        phase=PhaseSnapshotSlice(
            schema_version=1,
            domain_version=1,
            phase=GamePhase.LOBBY,
        ),
        setup=SetupSnapshotSlice(
            schema_version=1,
            domain_version=2,
            script_id="script-1",
            public_name="Public Script",
            manifest_reference="manifest-1",
            manifest_version=3,
        ),
        participants=ParticipantSnapshotSlice(
            schema_version=1,
            domain_version=1,
            participants=(
                ParticipantSnapshotRecord(
                    participant_id="dm-1",
                    participant_type=ParticipantType.DM,
                    membership_state=ParticipantMembershipState.ACTIVE,
                    character_id=None,
                    binding_version=1,
                ),
            ),
        ),
        game_rules=GameRuleSnapshotSlice(
            schema_version=2,
            domain_version=1,
            committed_rule_set_reference="rule-set:commit-1",
            committed_disclosure_state_reference="disclosure-state:commit-1",
        ),
        quest=QuestSnapshotSlice(1, 0, None, None, None),
        hidden_state=HiddenGameStateSlice(
            schema_version=1,
            domain_version=1,
            committed_state_reference="hidden-state:commit-1",
        ),
    )


def _activation_event() -> GameEvent:
    payload = RuleSetActivatedPayload(
        command_id="command-1",
        operation_id="operation-1",
        input_event_id="event-7",
        result_code="RULE_SET_ACTIVATED",
        result_state_version=5,
        manifest_reference="manifest-1",
        rule_set_domain_version=1,
        hidden_state_domain_version=1,
    )
    return GameEvent(
        event_id="result-1",
        game_id="game-1",
        session_id="session-1",
        event_type=GameEventType.RULE_SET_ACTIVATED,
        actor="session-actor",
        source=GameEventSource.CONTROL,
        correlation_id="correlation-1",
        timestamp=NOW,
        payload=payload.to_mapping(),
        visibility=EventVisibility.DM_CONTROL,
        observed_state_version=4,
        causation_event_id="event-7",
    )


def _mutation():
    return apply_contract.GameRuleMutation(
        mutation_type=apply_contract.GameRuleMutationType.ACTIVATE_RULE_SET,
        manifest_reference="manifest-1",
        committed_rule_set_reference="rule-set:commit-1",
        committed_disclosure_state_reference="disclosure-state:commit-1",
        opaque_hidden_state_reference="hidden-state:commit-1",
        expected_game_rule_version=0,
        resulting_game_rule_version=1,
        expected_hidden_state_version=0,
        resulting_hidden_state_version=1,
        provenance_reference="provenance-1",
    )


def _plan() -> ControlApplyPlan:
    return ControlApplyPlan(
        game_id="game-1",
        session_id="session-1",
        group_id="group-1",
        command_id="command-1",
        command_type=SessionCommandType.ACTIVATE_RULE_SET,
        operation_id="operation-1",
        operation_claim_id="claim-1",
        input_event_id="event-7",
        input_sequence_no=7,
        expected_state_version=4,
        expected_cursor=6,
        expected_binding_version=1,
        candidate_snapshot=_candidate(),
        participant_mutations=(),
        setup_mutations=(),
        ownership_intent=OwnershipIntent(
            intent_type=OwnershipIntentType.UNCHANGED,
            expected_generation=None,
            resulting_generation=None,
        ),
        result_events=(_activation_event(),),
        operation_terminal_state=ControlOperationStatus.SUCCESS,
        game_rule_mutations=(_mutation(),),
        game_rule_evidence=ControlGameRuleApplyEvidence(
            rule_set_activation=_activation_evidence()
        ),
    )


def test_activate_rule_set_plan_requires_exact_tri_bound_mutation_evidence_and_candidate() -> None:
    plan = _plan()

    assert plan.game_rule_mutations == (_mutation(),)
    assert (
        plan.game_rule_evidence.rule_set_activation
        == _activation_evidence()
    )
    assert plan.result_events[0].event_type is GameEventType.RULE_SET_ACTIVATED


@pytest.mark.parametrize(
    "replacement",
    [
        {"game_rule_mutations": ()},
        {"game_rule_mutations": (_mutation(), _mutation())},
        {"game_rule_evidence": ControlGameRuleApplyEvidence()},
        {
            "game_rule_evidence": ControlGameRuleApplyEvidence(
                rule_set_activation=_activation_evidence(
                    validation_status=GameRuleEvidenceStatus.UNKNOWN
                )
            )
        },
        {
            "game_rule_mutations": (
                replace(
                    _mutation(),
                    committed_rule_set_reference="rule-set:other",
                ),
            )
        },
        {
            "candidate_snapshot": replace(
                _candidate(),
                game_rules=replace(
                    _candidate().game_rules,
                    committed_rule_set_reference="rule-set:other",
                ),
            )
        },
    ],
)
def test_activate_rule_set_plan_rejects_partial_or_tampered_composition(
    replacement: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        replace(_plan(), **replacement)


def test_activate_rule_set_plan_rejects_duck_typed_incomplete_candidate_subclass() -> None:
    complete = _candidate()

    class IncompleteCandidateSnapshot(CandidateSessionSnapshot):
        __slots__ = ("setup", "game_rules", "hidden_state")

    incomplete = IncompleteCandidateSnapshot(
        game_id=complete.game_id,
        session_id=complete.session_id,
        group_id=complete.group_id,
        dm_participant_id=complete.dm_participant_id,
        status=complete.status,
        current_phase=complete.current_phase,
        state_version=complete.state_version,
        last_applied_sequence_no=complete.last_applied_sequence_no,
    )
    object.__setattr__(incomplete, "setup", complete.setup)
    object.__setattr__(incomplete, "game_rules", complete.game_rules)
    object.__setattr__(incomplete, "hidden_state", complete.hidden_state)

    with pytest.raises(ValueError, match="complete CandidateGameSnapshot"):
        replace(_plan(), candidate_snapshot=incomplete)


@pytest.mark.parametrize("slice_name", ("lifecycle", "participants"))
def test_activate_rule_set_plan_reconstructs_every_nested_candidate_slice(
    slice_name: str,
) -> None:
    candidate = _candidate()
    object.__setattr__(candidate, slice_name, object())

    with pytest.raises(ValueError, match="complete CandidateGameSnapshot"):
        replace(_plan(), candidate_snapshot=candidate)


def test_game_rule_plan_fields_are_backward_compatible_defaults_but_closed_by_command() -> None:
    plan_fields = {field.name: field for field in fields(ControlApplyPlan)}

    assert plan_fields["game_rule_mutations"].default == ()
    assert plan_fields["game_rule_evidence"].default_factory is not None
    with pytest.raises(ValueError):
        replace(
            _plan(),
            command_type=SessionCommandType.PAUSE_GAME,
            result_events=(
                replace(
                    _activation_event(),
                    event_type=GameEventType.SESSION_PAUSED,
                    visibility=EventVisibility.SYSTEM_ONLY,
                ),
            ),
        )


def test_game_rule_builder_module_and_public_reject_contract_exist() -> None:
    spec = importlib.util.find_spec(
        "game_runtime.session_control.game_rule_control_builder"
    )

    assert spec is not None
    assert spec.loader is not None


def test_game_rule_mutation_builder_and_reject_contracts_are_public() -> None:
    assert (
        session_control.GameRuleMutation
        is apply_contract.GameRuleMutation
    )
    assert (
        session_control.GameRuleMutationType
        is apply_contract.GameRuleMutationType
    )
    module = _builder_module()
    assert (
        session_control.GameRuleControlApplyPlanBuilder
        is module.GameRuleControlApplyPlanBuilder
    )
    assert (
        session_control.GameRuleControlRejectReason
        is module.GameRuleControlRejectReason
    )


def _context(
    *,
    setup_ready: bool = True,
    status: GameSessionStatus = GameSessionStatus.CREATED,
    phase: GamePhase = GamePhase.LOBBY,
    current_rule_reference: str | None = None,
    current_hidden_reference: str | None = None,
    requested_rule_reference: str = "rule-set:commit-1",
    requested_hidden_reference: str = "hidden-state:commit-1",
    evidence_status: GameRuleEvidenceStatus = GameRuleEvidenceStatus.VERIFIED,
) -> ControlApplyBuildContext:
    setup_version = 2 if setup_ready else 0
    game_rule_version = 0 if current_rule_reference is None else 1
    hidden_state_version = 0 if current_hidden_reference is None else 1
    payload = ActivateRuleSetPayload(
        manifest_reference="manifest-1",
        expected_setup_version=setup_version,
        expected_game_rule_version=game_rule_version,
        expected_hidden_state_version=hidden_state_version,
    )
    fingerprint = fingerprint_payload(payload)
    event = GameEvent(
        event_id="event-7",
        game_id="game-1",
        session_id="session-1",
        event_type=GameEventType.DM_COMMAND,
        actor="dm-1",
        source=GameEventSource.CONTROL,
        correlation_id="correlation-1",
        timestamp=NOW,
        payload=DMCommandEventPayload(
            command_id="command-1",
            command_type=SessionCommandType.ACTIVATE_RULE_SET,
            requester="dm-1",
            causation_event_id="request-1",
            observed_state_version=4,
            payload_reference=f"sha256:{fingerprint}",
            payload_fingerprint=fingerprint,
        ).to_mapping(),
        visibility=EventVisibility.DM_CONTROL,
        observed_state_version=4,
        causation_event_id="request-1",
    )
    envelope = ControlEventDeliveryEnvelope(
        event=event,
        event_sequence_no=7,
        operation_id="operation-1",
        command_id="command-1",
        observed_state_version=4,
        requester_principal_ref="dm-1",
        requester_binding_version=1,
        authorization_reference="authorization-1",
        confirmation_reference="confirmation-1",
        correlation_id="correlation-1",
        stored_event_reference="event-7",
    )
    participants = (
        ControlParticipantBuildView(
            game_id="game-1",
            session_id="session-1",
            participant_id="dm-1",
            participant_type=ParticipantType.DM,
            membership_state=ParticipantMembershipState.ACTIVE,
            character_id=None,
            binding_version=1,
        ),
    )
    setup_view = (
        ControlSetupBuildView(
            game_id="game-1",
            session_id="session-1",
            script_id="script-1",
            public_name="Public Script",
            manifest_reference="manifest-1",
            setup_version=2,
        )
        if setup_ready
        else None
    )
    snapshot = CandidateGameSnapshot(
        game_id="game-1",
        session_id="session-1",
        group_id="group-1",
        dm_participant_id="dm-1",
        status=status,
        current_phase=phase,
        state_version=4,
        last_applied_sequence_no=6,
        snapshot_schema_version=3,
        lifecycle=LifecycleSnapshotSlice(
            schema_version=1,
            domain_version=1,
            status=status,
        ),
        phase=PhaseSnapshotSlice(
            schema_version=1,
            domain_version=1,
            phase=phase,
        ),
        setup=(
            SetupSnapshotSlice(
                schema_version=1,
                domain_version=2,
                script_id="script-1",
                public_name="Public Script",
                manifest_reference="manifest-1",
                manifest_version=3,
            )
            if setup_ready
            else SetupSnapshotSlice(
                schema_version=1,
                domain_version=0,
                script_id=None,
                public_name=None,
                manifest_reference=None,
                manifest_version=None,
            )
        ),
        participants=ParticipantSnapshotSlice(
            schema_version=1,
            domain_version=1,
            participants=(
                ParticipantSnapshotRecord(
                    participant_id="dm-1",
                    participant_type=ParticipantType.DM,
                    membership_state=ParticipantMembershipState.ACTIVE,
                    character_id=None,
                    binding_version=1,
                ),
            ),
        ),
        game_rules=GameRuleSnapshotSlice(
            schema_version=2,
            domain_version=game_rule_version,
            committed_rule_set_reference=current_rule_reference,
            committed_disclosure_state_reference=(
                None
                if current_rule_reference is None
                else "disclosure-state:current"
            ),
        ),
        quest=QuestSnapshotSlice(1, 0, None, None, None),
        hidden_state=HiddenGameStateSlice(
            schema_version=1,
            domain_version=hidden_state_version,
            committed_state_reference=current_hidden_reference,
        ),
    )
    evidence = ControlGameRuleApplyEvidence(
        rule_set_activation=ControlRuleSetActivationEvidence(
            evidence_schema_version=1,
            game_id="game-1",
            session_id="session-1",
            observed_state_version=4,
            setup_manifest_reference="manifest-1",
            setup_version=setup_version,
            committed_rule_set_reference=requested_rule_reference,
            initial_disclosure_state_reference="disclosure-state:commit-1",
            rule_set_version=game_rule_version,
            opaque_hidden_state_reference=requested_hidden_reference,
            hidden_state_version=hidden_state_version,
            provenance_reference="provenance-1",
            validation_status=evidence_status,
        )
    )
    return ControlApplyBuildContext(
        envelope=envelope,
        claim=ControlOperationClaim(
            game_id="game-1",
            session_id="session-1",
            command_id="command-1",
            operation_id="operation-1",
            input_event_id="event-7",
            claim_id="claim-1",
            claimed_at=NOW,
        ),
        command_intent=CanonicalControlCommandIntent(
            intent_schema_version=1,
            command_type=SessionCommandType.ACTIVATE_RULE_SET,
            payload=payload,
            payload_fingerprint=fingerprint,
        ),
        session_view=ControlSessionBuildView(
            game_id="game-1",
            session_id="session-1",
            group_id="group-1",
            dm_participant_id="dm-1",
            status=status,
            current_phase=phase,
            state_version=4,
            last_applied_sequence_no=6,
            current_game_snapshot=snapshot,
            game_rule_evidence=evidence,
        ),
        participant_views=participants,
        setup_view=setup_view,
        ownership_evidence=ControlOwnershipBuildEvidence(
            game_id="game-1",
            session_id="session-1",
            group_id="group-1",
            active_generation=None,
            last_allocated_generation=0,
            observed_state_version=4,
        ),
        result_event_seed=ControlResultEventSeed(
            seed_contract_version=1,
            game_id="game-1",
            session_id="session-1",
            command_id="command-1",
            operation_id="operation-1",
            input_event_id="event-7",
            timestamp=NOW,
            correlation_id="correlation-1",
        ),
    )


def _builder_module():
    import game_runtime.session_control.game_rule_control_builder as module

    return module


def test_success_builds_complete_game_rule_candidate_and_preserves_other_slices() -> None:
    module = _builder_module()
    context = _context()
    current = context.session_view.current_game_snapshot
    assert current is not None

    outcome = module.GameRuleControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildPlanReady)
    plan = outcome.plan
    candidate = plan.candidate_snapshot
    assert isinstance(candidate, CandidateGameSnapshot)
    assert (
        candidate.game_id,
        candidate.session_id,
        candidate.group_id,
        candidate.dm_participant_id,
        candidate.status,
        candidate.current_phase,
    ) == (
        current.game_id,
        current.session_id,
        current.group_id,
        current.dm_participant_id,
        current.status,
        current.current_phase,
    )
    assert candidate.state_version == current.state_version + 1
    assert candidate.last_applied_sequence_no == context.envelope.event_sequence_no
    assert candidate.lifecycle is current.lifecycle
    assert candidate.phase is current.phase
    assert candidate.setup is current.setup
    assert candidate.participants is current.participants
    assert candidate.game_rules is not current.game_rules
    assert candidate.hidden_state is not current.hidden_state
    assert candidate.game_rules.domain_version == 1
    assert (
        candidate.game_rules.committed_rule_set_reference
        == "rule-set:commit-1"
    )
    assert candidate.hidden_state.domain_version == 1
    assert (
        candidate.hidden_state.committed_state_reference
        == "hidden-state:commit-1"
    )
    assert plan.participant_mutations == ()
    assert plan.setup_mutations == ()
    assert len(plan.game_rule_mutations) == 1
    assert plan.game_rule_mutations[0] == _mutation()
    assert plan.game_rule_evidence is context.session_view.game_rule_evidence
    assert plan.ownership_intent == OwnershipIntent(
        intent_type=OwnershipIntentType.UNCHANGED,
        expected_generation=None,
        resulting_generation=None,
    )


def test_success_emits_one_deterministic_privacy_minimal_activation_event() -> None:
    module = _builder_module()
    context = _context()
    first = module.GameRuleControlApplyPlanBuilder().build(context)
    second = module.GameRuleControlApplyPlanBuilder().build(context)

    assert first == second
    assert isinstance(first, BuildPlanReady)
    assert len(first.plan.result_events) == 1
    event = first.plan.result_events[0]
    assert event.event_type is GameEventType.RULE_SET_ACTIVATED
    assert event.event_id == context.result_event_seed.derive_event_id(
        GameEventType.RULE_SET_ACTIVATED,
        ordinal=1,
    )
    assert event.visibility is EventVisibility.DM_CONTROL
    payload = validate_control_result_event(event)
    assert isinstance(payload, RuleSetActivatedPayload)
    assert payload.to_mapping() == {
        "command_id": "command-1",
        "operation_id": "operation-1",
        "input_event_id": "event-7",
        "result_code": "RULE_SET_ACTIVATED",
        "result_state_version": 5,
        "manifest_reference": "manifest-1",
        "rule_set_domain_version": 1,
        "hidden_state_domain_version": 1,
    }
    serialized = repr(event.payload)
    assert "rule-set:commit-1" not in serialized
    assert "hidden-state:commit-1" not in serialized
    assert "opaque_hidden" not in serialized


@pytest.mark.parametrize(
    ("context", "reason"),
    [
        (_context(setup_ready=False), "SETUP_NOT_READY"),
        (
            _context(
                status=GameSessionStatus.RUNNING,
                phase=GamePhase.INTRODUCTION,
            ),
            "INVALID_LIFECYCLE",
        ),
        (
            _context(
                current_rule_reference="rule-set:commit-1",
                current_hidden_reference="hidden-state:commit-1",
            ),
            "RULE_SET_ALREADY_ACTIVE",
        ),
        (
            _context(
                current_rule_reference="rule-set:current",
                current_hidden_reference="hidden-state:current",
            ),
            "RULE_SET_CONFLICT",
        ),
    ],
)
def test_business_failures_build_exact_rejects(
    context: ControlApplyBuildContext,
    reason: str,
) -> None:
    module = _builder_module()

    outcome = module.GameRuleControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildReject)
    payload = validate_control_result_event(outcome.plan.rejection_event)
    assert isinstance(payload, SessionControlRejectedPayload)
    assert payload.reason_code == reason
    assert outcome.plan.command_type is SessionCommandType.ACTIVATE_RULE_SET


@pytest.mark.parametrize(
    "mutator",
    [
        lambda context: (
            object.__setattr__(
                context.session_view,
                "game_rule_evidence",
                ControlGameRuleApplyEvidence(),
            ),
            context,
        )[-1],
        lambda context: (
            object.__setattr__(
                context.session_view.game_rule_evidence.rule_set_activation,
                "validation_status",
                GameRuleEvidenceStatus.UNKNOWN,
            ),
            context,
        )[-1],
        lambda context: (
            object.__setattr__(
                context.command_intent.payload,
                "manifest_reference",
                "manifest:tampered",
            ),
            context,
        )[-1],
        lambda context: (
            object.__setattr__(
                context.session_view,
                "state_version",
                5,
            ),
            context,
        )[-1],
        lambda context: (
            object.__setattr__(
                context.session_view,
                "last_applied_sequence_no",
                5,
            ),
            context,
        )[-1],
        lambda context: (
            object.__setattr__(
                context.session_view.current_game_snapshot,
                "group_id",
                "other-group",
            ),
            context,
        )[-1],
        lambda context: (
            object.__setattr__(
                context.session_view.current_game_snapshot.setup,
                "manifest_reference",
                "manifest:tampered",
            ),
            context,
        )[-1],
        lambda context: (
            object.__setattr__(
                context.session_view.current_game_snapshot.participants,
                "participants",
                (),
            ),
            context,
        )[-1],
        lambda context: (
            object.__setattr__(context, "setup_view", None),
            context,
        )[-1],
        lambda context: (
            object.__setattr__(
                context.session_view.current_game_snapshot,
                "game_rules",
                object(),
            ),
            context,
        )[-1],
        lambda context: (
            object.__setattr__(
                context.command_intent.payload,
                "expected_game_rule_version",
                9,
            ),
            context,
        )[-1],
    ],
)
def test_missing_unknown_tampered_or_mismatched_inputs_fail_closed(
    mutator,
) -> None:
    module = _builder_module()

    outcome = module.GameRuleControlApplyPlanBuilder().build(mutator(_context()))

    assert isinstance(outcome, BuildNonCommit)
    assert outcome.reason in {
        BuildNonCommitReason.INVALID_CONTEXT,
        BuildNonCommitReason.EVIDENCE_MISMATCH,
        BuildNonCommitReason.UNKNOWN_SCHEMA,
    }


def test_tampered_command_payload_mismatch_is_invalid_context() -> None:
    module = _builder_module()
    context = _context()
    object.__setattr__(
        context.command_intent,
        "command_type",
        SessionCommandType.PAUSE_GAME,
    )

    outcome = module.GameRuleControlApplyPlanBuilder().build(context)

    assert outcome == BuildNonCommit(
        reason=BuildNonCommitReason.INVALID_CONTEXT,
        detail_code="CONTROL_APPLY_CONTEXT_INVALID",
    )


def test_extraneous_lifecycle_evidence_precedes_invalid_lifecycle_reject() -> None:
    module = _builder_module()
    context = _context(
        status=GameSessionStatus.RUNNING,
        phase=GamePhase.INTRODUCTION,
    )
    context = replace(
        context,
        lifecycle_evidence=ControlLifecycleApplyEvidence(
            phase_visibility_intent=ControlPhaseVisibilityIntent(
                contract_version=1,
                game_id="game-1",
                session_id="session-1",
                expected_state_version=4,
                previous_phase=GamePhase.INTRODUCTION,
                target_phase=GamePhase.EXPLORATION,
                visibility_policy_reference="visibility-policy-1",
                visibility_policy_version=1,
                decision=PhaseVisibilityDecision.NO_CHANGE,
                change_set_reference=None,
                intent_reference="visibility-intent-1",
            )
        ),
    )

    outcome = module.GameRuleControlApplyPlanBuilder().build(context)

    assert outcome == BuildNonCommit(
        reason=BuildNonCommitReason.EVIDENCE_MISMATCH,
        detail_code="ACTIVATE_RULE_SET_CONTEXT_EVIDENCE_INVALID",
    )


def test_extraneous_setup_evidence_precedes_setup_not_ready_reject() -> None:
    from test_setup_participant_apply_plan_builder import (
        _context as setup_context,
    )

    module = _builder_module()
    context = _context(setup_ready=False)
    object.__setattr__(
        context,
        "setup_participant_evidence",
        setup_context(
            SessionCommandType.SET_SCRIPT
        ).setup_participant_evidence,
    )

    outcome = module.GameRuleControlApplyPlanBuilder().build(context)

    assert outcome == BuildNonCommit(
        reason=BuildNonCommitReason.EVIDENCE_MISMATCH,
        detail_code="ACTIVATE_RULE_SET_CONTEXT_EVIDENCE_INVALID",
    )


@pytest.mark.parametrize(
    "evidence_case",
    ("missing", "unknown", "mismatched"),
)
def test_untrusted_game_rule_evidence_precedes_lifecycle_and_setup_rejects(
    evidence_case: str,
) -> None:
    module = _builder_module()
    context = _context(
        setup_ready=False,
        status=GameSessionStatus.RUNNING,
        phase=GamePhase.INTRODUCTION,
        evidence_status=(
            GameRuleEvidenceStatus.UNKNOWN
            if evidence_case == "unknown"
            else GameRuleEvidenceStatus.VERIFIED
        ),
    )
    activation = context.session_view.game_rule_evidence.rule_set_activation
    assert activation is not None
    if evidence_case == "missing":
        object.__setattr__(
            context.session_view,
            "game_rule_evidence",
            ControlGameRuleApplyEvidence(),
        )
    elif evidence_case == "mismatched":
        object.__setattr__(activation, "setup_version", 1)

    outcome = module.GameRuleControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildNonCommit)
    assert not isinstance(outcome, BuildReject)


def test_tampered_command_does_not_bypass_full_context_revalidation() -> None:
    module = _builder_module()
    context = _context()
    object.__setattr__(
        context.command_intent,
        "command_type",
        SessionCommandType.PAUSE_GAME,
    )
    object.__setattr__(context.session_view, "state_version", 5)

    outcome = module.GameRuleControlApplyPlanBuilder().build(context)

    assert outcome == BuildNonCommit(
        reason=BuildNonCommitReason.INVALID_CONTEXT,
        detail_code="CONTROL_APPLY_CONTEXT_INVALID",
    )


def test_post_init_mutable_participant_view_list_cannot_reach_plan_ready() -> None:
    module = _builder_module()
    context = _context()
    object.__setattr__(
        context,
        "participant_views",
        list(context.participant_views),
    )

    outcome = module.GameRuleControlApplyPlanBuilder().build(context)

    assert outcome == BuildNonCommit(
        reason=BuildNonCommitReason.INVALID_CONTEXT,
        detail_code="CONTROL_APPLY_CONTEXT_INVALID",
    )


def test_malformed_transition_decision_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _builder_module()
    monkeypatch.setattr(
        module,
        "transition_game_rule_activation",
        lambda request: object(),
    )

    outcome = module.GameRuleControlApplyPlanBuilder().build(_context())

    assert isinstance(outcome, BuildNonCommit)
    assert outcome.detail_code == "ACTIVATE_RULE_SET_TRANSITION_INVALID"


def test_private_composer_cannot_bypass_full_validation() -> None:
    module = _builder_module()
    context = _context()
    current = context.session_view.current_game_snapshot
    evidence = context.session_view.game_rule_evidence.rule_set_activation
    assert current is not None
    assert evidence is not None

    outcome = module._compose_game_rule_apply_plan(
        context=context,
        current=current,
        decision=object(),
        evidence=evidence,
    )

    assert outcome == BuildNonCommit(
        reason=BuildNonCommitReason.INVALID_CONTEXT,
        detail_code="ACTIVATE_RULE_SET_COMPOSITION_INVALID",
    )


def test_builder_is_sync_stateless_and_has_no_forbidden_dependencies() -> None:
    module = _builder_module()
    source = inspect.getsource(module)
    tree = ast.parse(source)
    imports = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    forbidden = (
        "actor",
        "coordinator",
        "persistence",
        "event_store",
        "recovery",
        "notification",
        "plugin",
        "tool",
        "llm",
        "asyncio",
    )

    assert tuple(member.name for member in module.GameRuleControlRejectReason) == (
        "INVALID_LIFECYCLE",
        "SETUP_NOT_READY",
        "RULE_SET_ALREADY_ACTIVE",
        "RULE_SET_CONFLICT",
        "INVALID_PHASE",
        "RULE_SET_NOT_ACTIVE",
        "CLUE_ALREADY_REVEALED",
        "CLUE_NOT_FOUND",
        "CLUE_NOT_REVEALABLE",
    )
    assert module.GameRuleControlApplyPlanBuilder.__slots__ == ()
    assert not inspect.iscoroutinefunction(
        module.GameRuleControlApplyPlanBuilder.build
    )
    assert not inspect.iscoroutinefunction(module._compose_game_rule_apply_plan)
    assert not any(
        token in imported.casefold()
        for imported in imports
        for token in forbidden
    )
