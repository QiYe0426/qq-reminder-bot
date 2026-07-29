from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, fields, replace
import inspect

import pytest

from game_runtime.event import GameEventType, validate_control_result_event
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control import (
    ActorOwnedApplyCoordinator,
    BuildNonCommit,
    BuildNonCommitReason,
    BuildPlanReady,
    BuildReject,
    ControlLifecycleApplyEvidence,
    ControlLifecycleEvidenceError,
    CoordinatorCommitReturned,
    LifecycleControlApplyPlanBuilder,
    OwnershipIntentType,
    SessionCommandType,
    StartReadinessStatus,
)
from test_lifecycle_phase_apply_plan_builder import (
    _end_evidence,
    _phase_evidence,
    _start_evidence,
    make_context,
)
from test_apply_coordinator import FakeApplyPort, make_evidence, run


def _start_apply_evidence(
    readiness_status: StartReadinessStatus = StartReadinessStatus.READY,
) -> ControlLifecycleApplyEvidence:
    return ControlLifecycleApplyEvidence(
        start_readiness=replace(
            _start_evidence(4),
            readiness_status=readiness_status,
        ),
        phase_visibility_intent=_phase_evidence(
            4,
            GamePhase.LOBBY,
            GamePhase.INTRODUCTION,
        ),
    )


def test_lifecycle_evidence_is_immutable_value_only() -> None:
    evidence = (
        _start_evidence(4),
        ControlLifecycleApplyEvidence(),
        _end_evidence(4),
    )
    forbidden = (
        "gamesession",
        "candidatesessionsnapshot",
        "port",
        "callback",
        "task",
        "repository",
    )

    for value in evidence:
        assert not hasattr(value, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(value, fields(value)[0].name, None)
        for field in fields(value):
            contract = f"{field.name} {field.type}".lower()
            assert not any(term in contract for term in forbidden)


@pytest.mark.parametrize(
    ("command_type", "evidence"),
    [
        (SessionCommandType.START_GAME, _start_apply_evidence()),
        (SessionCommandType.PAUSE_GAME, ControlLifecycleApplyEvidence()),
        (
            SessionCommandType.END_GAME,
            ControlLifecycleApplyEvidence(end_retention=_end_evidence(4)),
        ),
    ],
)
def test_lifecycle_evidence_accepts_exact_start_pause_end_sets(
    command_type: SessionCommandType,
    evidence: ControlLifecycleApplyEvidence,
) -> None:
    evidence.validate_for_command(
        command_type,
        game_id="game-1",
        session_id="session-1",
        observed_state_version=4,
    )


@pytest.mark.parametrize(
    ("command_type", "evidence"),
    [
        (SessionCommandType.START_GAME, ControlLifecycleApplyEvidence()),
        (
            SessionCommandType.PAUSE_GAME,
            ControlLifecycleApplyEvidence(end_retention=_end_evidence(4)),
        ),
        (SessionCommandType.END_GAME, ControlLifecycleApplyEvidence()),
    ],
)
def test_lifecycle_evidence_rejects_missing_or_extraneous_sets(
    command_type: SessionCommandType,
    evidence: ControlLifecycleApplyEvidence,
) -> None:
    with pytest.raises(ControlLifecycleEvidenceError):
        evidence.validate_for_command(
            command_type,
            game_id="game-1",
            session_id="session-1",
            observed_state_version=4,
        )


@pytest.mark.parametrize(
    ("game_id", "session_id", "state_version"),
    [
        ("other-game", "session-1", 4),
        ("game-1", "other-session", 4),
        ("game-1", "session-1", 5),
    ],
)
def test_lifecycle_evidence_binds_scope_and_version(
    game_id: str,
    session_id: str,
    state_version: int,
) -> None:
    evidence = ControlLifecycleApplyEvidence(end_retention=_end_evidence(4))

    with pytest.raises(ControlLifecycleEvidenceError):
        evidence.validate_for_command(
            SessionCommandType.END_GAME,
            game_id=game_id,
            session_id=session_id,
            observed_state_version=state_version,
        )


def test_start_builds_running_introduction_plan() -> None:
    context = make_context(
        SessionCommandType.START_GAME,
        status=GameSessionStatus.CREATED,
        current_phase=GamePhase.LOBBY,
    )

    outcome = LifecycleControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildPlanReady)
    assert outcome.plan.candidate_snapshot.status is GameSessionStatus.RUNNING
    assert outcome.plan.candidate_snapshot.current_phase is GamePhase.INTRODUCTION
    assert outcome.plan.ownership_intent.intent_type is OwnershipIntentType.ACQUIRE
    assert tuple(event.event_type for event in outcome.plan.result_events) == (
        GameEventType.SESSION_STARTED,
        GameEventType.PHASE_CHANGED,
    )


@pytest.mark.parametrize(
    ("readiness", "outcome_type", "reason"),
    [
        (
            StartReadinessStatus.NOT_READY,
            BuildReject,
            "START_NOT_READY",
        ),
        (
            StartReadinessStatus.UNKNOWN,
            BuildNonCommit,
            BuildNonCommitReason.EVIDENCE_MISMATCH,
        ),
    ],
)
def test_start_not_ready_or_unknown_fails_closed(
    readiness: StartReadinessStatus,
    outcome_type: type[BuildReject] | type[BuildNonCommit],
    reason: str | BuildNonCommitReason,
) -> None:
    context = make_context(
        SessionCommandType.START_GAME,
        status=GameSessionStatus.CREATED,
        current_phase=GamePhase.LOBBY,
        lifecycle_evidence=_start_apply_evidence(readiness),
    )

    outcome = LifecycleControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, outcome_type)
    if isinstance(outcome, BuildReject):
        payload = validate_control_result_event(outcome.plan.rejection_event)
        assert payload.reason_code == reason
    else:
        assert outcome.reason is reason
        assert not hasattr(outcome, "plan")


def test_pause_builds_paused_plan_without_changing_phase() -> None:
    context = make_context(
        SessionCommandType.PAUSE_GAME,
        status=GameSessionStatus.RUNNING,
        current_phase=GamePhase.EXPLORATION,
    )

    outcome = LifecycleControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildPlanReady)
    assert outcome.plan.candidate_snapshot.status is GameSessionStatus.PAUSED
    assert outcome.plan.candidate_snapshot.current_phase is GamePhase.EXPLORATION
    assert outcome.plan.ownership_intent.intent_type is OwnershipIntentType.RETAIN
    assert tuple(event.event_type for event in outcome.plan.result_events) == (
        GameEventType.SESSION_PAUSED,
    )


def test_pause_from_invalid_state_builds_business_reject() -> None:
    context = make_context(
        SessionCommandType.PAUSE_GAME,
        status=GameSessionStatus.PAUSED,
        current_phase=GamePhase.EXPLORATION,
    )

    outcome = LifecycleControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildReject)
    assert not hasattr(outcome.plan, "candidate_snapshot")
    payload = validate_control_result_event(outcome.plan.rejection_event)
    assert payload.reason_code == "INVALID_LIFECYCLE_TRANSITION"
    assert payload.result_state_version == context.session_view.state_version


@pytest.mark.parametrize(
    ("status", "active_generation", "expected_intent"),
    [
        (GameSessionStatus.CREATED, None, OwnershipIntentType.UNCHANGED),
        (GameSessionStatus.RUNNING, 3, OwnershipIntentType.RELEASE),
        (GameSessionStatus.PAUSED, 3, OwnershipIntentType.RELEASE),
    ],
)
def test_end_builds_frozen_plan_for_allowed_states(
    status: GameSessionStatus,
    active_generation: int | None,
    expected_intent: OwnershipIntentType,
) -> None:
    context = make_context(
        SessionCommandType.END_GAME,
        status=status,
        current_phase=(
            GamePhase.LOBBY
            if status is GameSessionStatus.CREATED
            else GamePhase.DISCUSSION
        ),
        active_generation=active_generation,
    )

    outcome = LifecycleControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildPlanReady)
    assert outcome.plan.candidate_snapshot.status is GameSessionStatus.ENDED
    assert outcome.plan.candidate_snapshot.current_phase is GamePhase.ENDING
    assert outcome.plan.ownership_intent.intent_type is expected_intent
    assert tuple(event.event_type for event in outcome.plan.result_events) == (
        GameEventType.SESSION_ENDED,
    )


def test_end_when_already_ended_builds_business_reject() -> None:
    context = make_context(
        SessionCommandType.END_GAME,
        status=GameSessionStatus.ENDED,
        current_phase=GamePhase.ENDING,
        active_generation=None,
    )

    outcome = LifecycleControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildReject)
    payload = validate_control_result_event(outcome.plan.rejection_event)
    assert payload.reason_code == "SESSION_ALREADY_ENDED"


def test_stale_lifecycle_command_builds_frozen_reject() -> None:
    context = make_context(
        SessionCommandType.PAUSE_GAME,
        status=GameSessionStatus.RUNNING,
        current_phase=GamePhase.EXPLORATION,
        session_version=5,
        command_observed_version=4,
    )

    outcome = LifecycleControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildReject)
    payload = validate_control_result_event(outcome.plan.rejection_event)
    assert payload.reason_code == "STALE_STATE_VERSION"
    assert payload.result_state_version == 5


def test_lifecycle_builder_is_deterministic() -> None:
    context = make_context(
        SessionCommandType.START_GAME,
        status=GameSessionStatus.CREATED,
        current_phase=GamePhase.LOBBY,
    )
    builder = LifecycleControlApplyPlanBuilder()

    first = builder.build(context)
    second = builder.build(context)

    assert isinstance(first, BuildPlanReady)
    assert isinstance(second, BuildPlanReady)
    assert first.plan == second.plan
    assert first.plan is not second.plan
    assert tuple(event.event_id for event in first.plan.result_events) == tuple(
        event.event_id for event in second.plan.result_events
    )


@pytest.mark.parametrize(
    "command_type",
    [SessionCommandType.RESUME_GAME, SessionCommandType.CHANGE_PHASE],
)
def test_lifecycle_builder_does_not_enter_resume_or_general_phase_apply(
    command_type: SessionCommandType,
) -> None:
    context = make_context(
        command_type,
        status=(
            GameSessionStatus.PAUSED
            if command_type is SessionCommandType.RESUME_GAME
            else GameSessionStatus.RUNNING
        ),
        current_phase=GamePhase.EXPLORATION,
    )

    outcome = LifecycleControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildNonCommit)
    assert outcome.reason is BuildNonCommitReason.REDUCER_UNAVAILABLE
    assert not hasattr(outcome, "plan")


def test_lifecycle_builder_has_no_apply_or_runtime_dependencies() -> None:
    builder = LifecycleControlApplyPlanBuilder()
    tree = ast.parse(inspect.getsource(LifecycleControlApplyPlanBuilder))
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert called_names.isdisjoint(
        {"reduce", "apply", "commit", "append", "notify", "recover", "retry"}
    )
    assert called_attributes.isdisjoint(
        {
            "atomic_apply",
            "commit_control_apply",
            "commit_control_rejection",
            "append",
            "notify",
            "recover",
            "retry",
        }
    )
    assert vars(builder) == {}


def test_existing_coordinator_accepts_lifecycle_builder_without_bypass() -> None:
    port = FakeApplyPort()
    coordinator = ActorOwnedApplyCoordinator(
        apply_port=port,
        builder=LifecycleControlApplyPlanBuilder(),
    )

    outcome = run(coordinator.coordinate(make_evidence()))

    assert isinstance(outcome, CoordinatorCommitReturned)
    assert [name for name, _ in port.calls] == [
        "claim_operation",
        "commit_control_apply",
    ]
