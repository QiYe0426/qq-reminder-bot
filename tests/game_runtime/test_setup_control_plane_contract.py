from __future__ import annotations

import ast
from dataclasses import replace
import inspect

import pytest

import game_runtime.session_control.setup_control_builder as builder_module
from game_runtime.event import (
    GameEventType,
    ScriptSetPayload,
    SessionControlRejectedPayload,
    validate_control_result_event,
)
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control import (
    BuildNonCommit,
    BuildNonCommitReason,
    BuildPlanReady,
    BuildReject,
    CandidateGameSnapshot,
    ControlSetupBuildView,
    ControlSetupParticipantApplyEvidence,
    GameRuleSnapshotSlice,
    HiddenGameStateSlice,
    LifecycleSnapshotSlice,
    OwnershipIntentType,
    ParticipantSnapshotRecord,
    ParticipantSnapshotSlice,
    PhaseSnapshotSlice,
    SessionCommandType,
    SetupControlApplyPlanBuilder,
    SetupMutationType,
    SetupSnapshotSlice,
)
from test_setup_participant_apply_plan_builder import _context as legacy_context


def _context(
    *,
    existing: bool = False,
    same_identity: bool = False,
    confirmation: str | None = "confirmation-1",
    status: GameSessionStatus = GameSessionStatus.CREATED,
    phase: GamePhase = GamePhase.LOBBY,
    manifest_version: int = 3,
):
    setup_view = (
        ControlSetupBuildView(
            game_id="game-1",
            session_id="session-1",
            script_id="script-2" if same_identity else "script-1",
            public_name=(
                "Public Script 2" if same_identity else "Public Script 1"
            ),
            manifest_reference=(
                "manifest-2" if same_identity else "manifest-1"
            ),
            setup_version=2,
        )
        if existing
        else None
    )
    context = legacy_context(
        SessionCommandType.SET_SCRIPT,
        status=status,
        setup_view=setup_view,
    )
    context = replace(
        context,
        envelope=replace(
            context.envelope,
            confirmation_reference=confirmation,
        ),
        session_view=replace(
            context.session_view,
            status=status,
            current_phase=phase,
        ),
    )
    records = tuple(
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
    setup = (
        SetupSnapshotSlice(
            schema_version=1,
            domain_version=0,
            script_id=None,
            public_name=None,
            manifest_reference=None,
            manifest_version=None,
        )
        if setup_view is None
        else SetupSnapshotSlice(
            schema_version=1,
            domain_version=setup_view.setup_version,
            script_id=setup_view.script_id,
            public_name=setup_view.public_name,
            manifest_reference=setup_view.manifest_reference,
            manifest_version=2,
        )
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
        snapshot_schema_version=1,
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
        setup=setup,
        participants=ParticipantSnapshotSlice(
            schema_version=1,
            domain_version=1,
            participants=records,
        ),
        game_rules=GameRuleSnapshotSlice(
            schema_version=1,
            domain_version=0,
            committed_rule_set_reference=None,
        ),
        hidden_state=HiddenGameStateSlice(
            schema_version=1,
            domain_version=0,
            committed_state_reference=None,
        ),
    )
    script_evidence = context.setup_participant_evidence.script_apply
    assert script_evidence is not None
    script_evidence = replace(
        script_evidence,
        manifest_version=manifest_version,
        expected_setup_version=setup.domain_version,
        resulting_setup_version=setup.domain_version + 1,
        ownership_generation=context.ownership_evidence.active_generation,
    )
    return replace(
        context,
        session_view=replace(
            context.session_view,
            current_game_snapshot=snapshot,
        ),
        setup_participant_evidence=ControlSetupParticipantApplyEvidence(
            script_apply=script_evidence,
        ),
    )


@pytest.mark.parametrize("existing", (False, True))
def test_success_builds_complete_composite_setup_plan(existing: bool) -> None:
    context = _context(existing=existing)
    current = context.session_view.current_game_snapshot
    assert current is not None

    outcome = SetupControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildPlanReady)
    plan = outcome.plan
    candidate = plan.candidate_snapshot
    assert isinstance(candidate, CandidateGameSnapshot)
    assert candidate.lifecycle is current.lifecycle
    assert candidate.phase is current.phase
    assert candidate.participants is current.participants
    assert candidate.game_rules is current.game_rules
    assert candidate.hidden_state is current.hidden_state
    assert candidate.setup is not current.setup
    assert candidate.setup.schema_version == current.setup.schema_version
    assert candidate.setup.domain_version == current.setup.domain_version + 1
    assert candidate.setup.script_id == "script-2"
    assert candidate.setup.public_name == "Public Script 2"
    assert candidate.setup.manifest_reference == "manifest-2"
    assert candidate.setup.manifest_version == 3
    assert candidate.state_version == current.state_version + 1
    assert candidate.last_applied_sequence_no == 7
    assert plan.participant_mutations == ()
    assert len(plan.setup_mutations) == 1
    assert plan.setup_mutations[0].mutation_type is SetupMutationType.SET_SCRIPT
    assert plan.ownership_intent.intent_type is OwnershipIntentType.UNCHANGED
    assert plan.ownership_intent.expected_generation is None
    assert plan.ownership_intent.resulting_generation is None
    assert plan.setup_participant_evidence is context.setup_participant_evidence


def test_success_emits_exact_script_set_event() -> None:
    context = _context()

    outcome = SetupControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildPlanReady)
    assert len(outcome.plan.result_events) == 1
    event = outcome.plan.result_events[0]
    assert event.event_type is GameEventType.SCRIPT_SET
    assert event.event_id == context.result_event_seed.derive_event_id(
        GameEventType.SCRIPT_SET,
        ordinal=1,
    )
    payload = validate_control_result_event(event)
    assert isinstance(payload, ScriptSetPayload)
    assert payload.result_code == "SET_SCRIPT_APPLIED"
    assert payload.result_state_version == 5
    assert payload.script_id == "script-2"
    assert payload.public_name == "Public Script 2"
    assert payload.manifest_reference == "manifest-2"


@pytest.mark.parametrize(
    ("context", "reason"),
    [
        (
            _context(
                status=GameSessionStatus.RUNNING,
                phase=GamePhase.INTRODUCTION,
            ),
            "INVALID_LIFECYCLE",
        ),
        (
            _context(
                status=GameSessionStatus.ENDED,
                phase=GamePhase.ENDING,
            ),
            "INVALID_LIFECYCLE",
        ),
        (
            _context(
                existing=True,
                same_identity=True,
                manifest_version=2,
            ),
            "SCRIPT_ALREADY_SET",
        ),
        (
            _context(existing=True, confirmation=None),
            "SCRIPT_REPLACEMENT_NOT_CONFIRMED",
        ),
    ],
)
def test_business_failures_build_reject(context, reason: str) -> None:
    outcome = SetupControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildReject)
    payload = validate_control_result_event(outcome.plan.rejection_event)
    assert isinstance(payload, SessionControlRejectedPayload)
    assert payload.reason_code == reason
    assert outcome.plan.expected_state_version == 4
    assert outcome.plan.expected_cursor == 6


def test_duplicate_precedes_missing_replacement_confirmation() -> None:
    outcome = SetupControlApplyPlanBuilder().build(
        _context(
            existing=True,
            same_identity=True,
            confirmation=None,
            manifest_version=2,
        )
    )

    assert isinstance(outcome, BuildReject)
    payload = validate_control_result_event(outcome.plan.rejection_event)
    assert isinstance(payload, SessionControlRejectedPayload)
    assert payload.reason_code == "SCRIPT_ALREADY_SET"


def test_paused_fails_closed_for_repair_evidence() -> None:
    outcome = SetupControlApplyPlanBuilder().build(
        _context(status=GameSessionStatus.PAUSED)
    )

    assert outcome == BuildNonCommit(
        reason=BuildNonCommitReason.RECOVERY_REQUIRED,
        detail_code="SET_SCRIPT_REPAIR_EVIDENCE_REQUIRED",
    )


def test_same_manifest_reference_with_new_manifest_version_fails_closed() -> None:
    context = _context(existing=True, manifest_version=3)
    current = context.session_view.current_game_snapshot
    assert current is not None
    context = replace(
        context,
        setup_view=replace(
            context.setup_view,
            manifest_reference="manifest-2",
        ),
        session_view=replace(
            context.session_view,
            current_game_snapshot=replace(
                current,
                setup=replace(
                    current.setup,
                    manifest_reference="manifest-2",
                ),
            ),
        ),
    )

    outcome = SetupControlApplyPlanBuilder().build(context)

    assert outcome == BuildNonCommit(
        reason=BuildNonCommitReason.RECOVERY_REQUIRED,
        detail_code="SET_SCRIPT_MANIFEST_VERSION_MISMATCH",
    )


@pytest.mark.parametrize(
    ("mutator", "detail"),
    [
        (
            lambda context: (
                object.__setattr__(
                    context.ownership_evidence,
                    "active_generation",
                    1,
                ),
                object.__setattr__(
                    context.setup_participant_evidence.script_apply,
                    "ownership_generation",
                    1,
                ),
                context,
            )[-1],
            "SET_SCRIPT_OWNERSHIP_INVALID",
        ),
        (
            lambda context: (
                object.__setattr__(
                    context,
                    "setup_participant_evidence",
                    ControlSetupParticipantApplyEvidence(),
                ),
                context,
            )[-1],
            "SET_SCRIPT_EVIDENCE_INCOMPLETE",
        ),
        (
            lambda context: (
                object.__setattr__(
                    context.setup_participant_evidence.script_apply,
                    "expected_setup_version",
                    9,
                ),
                object.__setattr__(
                    context.setup_participant_evidence.script_apply,
                    "resulting_setup_version",
                    10,
                ),
                context,
            )[-1],
            "CONTROL_APPLY_CONTEXT_INVALID",
        ),
        (
            lambda context: (
                object.__setattr__(
                    context.session_view,
                    "last_applied_sequence_no",
                    5,
                ),
                context,
            )[-1],
            "CONTROL_APPLY_CONTEXT_INVALID",
        ),
    ],
)
def test_invalid_evidence_and_bindings_fail_closed(mutator, detail: str) -> None:
    outcome = SetupControlApplyPlanBuilder().build(mutator(_context()))

    assert isinstance(outcome, BuildNonCommit)
    assert outcome.detail_code == detail


def test_setup_view_must_match_authoritative_snapshot() -> None:
    context = _context(existing=True)
    context = replace(
        context,
        setup_view=replace(context.setup_view, public_name="wrong"),
    )

    outcome = SetupControlApplyPlanBuilder().build(context)

    assert outcome == BuildNonCommit(
        reason=BuildNonCommitReason.EVIDENCE_MISMATCH,
        detail_code="COMPOSITE_SETUP_BINDING_MISMATCH",
    )


def test_deterministic_and_stateless() -> None:
    context = _context()
    builder = SetupControlApplyPlanBuilder()

    assert builder.build(context) == builder.build(context)
    assert SetupControlApplyPlanBuilder.__slots__ == ()
    assert not inspect.iscoroutinefunction(builder.build)


def test_builder_forbidden_dependencies_and_private_composition_helper() -> None:
    source = inspect.getsource(builder_module)
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
    )
    assert not any(
        token in module.lower()
        for module in imports
        for token in forbidden
    )
    assert hasattr(builder_module, "_compose_setup_apply_plan")
    assert not inspect.iscoroutinefunction(
        builder_module._compose_setup_apply_plan
    )


def test_private_composition_helper_fails_closed_on_incomplete_values() -> None:
    context = _context()
    current = context.session_view.current_game_snapshot
    evidence = context.setup_participant_evidence.script_apply
    assert current is not None
    assert evidence is not None

    outcome = builder_module._compose_setup_apply_plan(
        context=context,
        current=current,
        decision=object(),
        evidence=evidence,
    )

    assert outcome == BuildNonCommit(
        reason=BuildNonCommitReason.INVALID_CONTEXT,
        detail_code="SET_SCRIPT_COMPOSITION_INVALID",
    )
