from __future__ import annotations

import ast
from dataclasses import replace
import inspect
from types import SimpleNamespace

import pytest

from game_runtime.event import GameEventType
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control import (
    BuildNonCommit,
    BuildNonCommitReason,
    BuildPlanReady,
    BuildReject,
    ControlApplyBuildContext,
    ControlLifecycleApplyEvidence,
    LifecyclePhaseControlApplyPlanBuilder,
    OwnershipIntentType,
    SessionCommandType,
    StartReadinessStatus,
)
from test_lifecycle_phase_apply_plan_builder import (
    _phase_evidence,
    _start_evidence,
    make_context,
)


def test_repeated_success_build_has_identical_plan_and_event_identity() -> None:
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
    assert first.plan is not second.plan
    assert first.plan == second.plan
    assert first.plan.result_events == second.plan.result_events
    assert tuple(event.event_id for event in first.plan.result_events) == tuple(
        event.event_id for event in second.plan.result_events
    )


def test_repeated_reject_build_has_identical_plan_and_reject_identity() -> None:
    context = make_context(
        SessionCommandType.PAUSE_GAME,
        status=GameSessionStatus.PAUSED,
        current_phase=GamePhase.EXPLORATION,
    )
    builder = LifecyclePhaseControlApplyPlanBuilder()

    first = builder.build(context)
    second = builder.build(context)

    assert isinstance(first, BuildReject)
    assert isinstance(second, BuildReject)
    assert first.plan is not second.plan
    assert first.plan == second.plan
    assert first.plan.rejection_event.event_id == second.plan.rejection_event.event_id
    assert first.plan.rejection_event.event_id == (
        context.result_event_seed.derive_event_id(
            GameEventType.SESSION_CONTROL_REJECTED,
            ordinal=1,
        )
    )


def test_builder_is_stateless_and_has_no_forbidden_runtime_dependencies() -> None:
    builder = LifecyclePhaseControlApplyPlanBuilder()
    module = inspect.getmodule(LifecyclePhaseControlApplyPlanBuilder)
    assert module is not None
    tree = ast.parse(inspect.getsource(module))

    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    forbidden_module_parts = (
        ".actor",
        ".persistence",
        ".recovery",
        ".interfaces",
        "sqlite",
    )
    assert not any(
        part in imported
        for imported in imported_modules
        for part in forbidden_module_parts
    )

    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called_attributes.isdisjoint(
        {
            "atomic_apply",
            "claim_operation",
            "commit_control_apply",
            "commit_control_rejection",
            "apply_event",
            "transition_to",
            "transition_phase_to",
        }
    )
    assert vars(builder) == {}


def _unsupported_context(command_type: SessionCommandType) -> ControlApplyBuildContext:
    """Model a routing-boundary breach without constructing executable evidence."""

    context = object.__new__(ControlApplyBuildContext)
    object.__setattr__(
        context,
        "command_intent",
        SimpleNamespace(
            intent_schema_version=1,
            command_type=command_type,
        ),
    )
    return context


@pytest.mark.parametrize(
    "command_type",
    [
        SessionCommandType.CREATE_SESSION,
        SessionCommandType.SET_SCRIPT,
        SessionCommandType.ASSIGN_CHARACTER,
        SessionCommandType.REPLACE_PLAYER,
    ],
)
def test_non_lifecycle_commands_fail_closed_without_plan(
    command_type: SessionCommandType,
) -> None:
    outcome = LifecyclePhaseControlApplyPlanBuilder().build(
        _unsupported_context(command_type)
    )

    assert isinstance(outcome, BuildNonCommit)
    assert outcome.reason is BuildNonCommitReason.REDUCER_UNAVAILABLE
    assert not hasattr(outcome, "plan")


def test_missing_evidence_returns_noncommit_without_reject_event() -> None:
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


def test_unknown_evidence_returns_noncommit_without_reject_event() -> None:
    readiness = replace(
        _start_evidence(4),
        readiness_status=StartReadinessStatus.UNKNOWN,
    )
    evidence = ControlLifecycleApplyEvidence(
        start_readiness=readiness,
        phase_visibility_intent=_phase_evidence(
            4,
            GamePhase.LOBBY,
            GamePhase.INTRODUCTION,
        ),
    )
    context = make_context(
        SessionCommandType.START_GAME,
        status=GameSessionStatus.CREATED,
        current_phase=GamePhase.LOBBY,
        lifecycle_evidence=evidence,
    )

    outcome = LifecyclePhaseControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildNonCommit)
    assert outcome.reason is BuildNonCommitReason.EVIDENCE_MISMATCH
    assert not hasattr(outcome, "plan")


def test_mismatched_evidence_returns_noncommit_without_reject_event() -> None:
    readiness = replace(
        _start_evidence(4),
        setup_manifest_reference="other-manifest",
    )
    evidence = ControlLifecycleApplyEvidence(
        start_readiness=readiness,
        phase_visibility_intent=_phase_evidence(
            4,
            GamePhase.LOBBY,
            GamePhase.INTRODUCTION,
        ),
    )
    context = make_context(
        SessionCommandType.START_GAME,
        status=GameSessionStatus.CREATED,
        current_phase=GamePhase.LOBBY,
        lifecycle_evidence=evidence,
    )

    outcome = LifecyclePhaseControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildNonCommit)
    assert outcome.reason is BuildNonCommitReason.EVIDENCE_MISMATCH
    assert not hasattr(outcome, "plan")


def test_result_events_bind_seed_causation_correlation_and_stable_ordinals() -> None:
    context = make_context(
        SessionCommandType.START_GAME,
        status=GameSessionStatus.CREATED,
        current_phase=GamePhase.LOBBY,
    )

    outcome = LifecyclePhaseControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildPlanReady)
    started, changed = outcome.plan.result_events
    assert started.event_id == context.result_event_seed.derive_event_id(
        GameEventType.SESSION_STARTED,
        ordinal=1,
    )
    assert changed.event_id == context.result_event_seed.derive_event_id(
        GameEventType.PHASE_CHANGED,
        ordinal=2,
    )
    for event in (started, changed):
        assert event.causation_event_id == context.envelope.event.event_id
        assert event.correlation_id == context.result_event_seed.correlation_id
        assert event.timestamp == context.result_event_seed.timestamp
        assert not hasattr(event, "sequence_no")


@pytest.mark.parametrize(
    ("command_type", "status", "phase", "expected_intent"),
    [
        (
            SessionCommandType.START_GAME,
            GameSessionStatus.CREATED,
            GamePhase.LOBBY,
            OwnershipIntentType.ACQUIRE,
        ),
        (
            SessionCommandType.PAUSE_GAME,
            GameSessionStatus.RUNNING,
            GamePhase.EXPLORATION,
            OwnershipIntentType.RETAIN,
        ),
        (
            SessionCommandType.RESUME_GAME,
            GameSessionStatus.PAUSED,
            GamePhase.DISCUSSION,
            OwnershipIntentType.RETAIN,
        ),
        (
            SessionCommandType.CHANGE_PHASE,
            GameSessionStatus.RUNNING,
            GamePhase.EXPLORATION,
            OwnershipIntentType.RETAIN,
        ),
        (
            SessionCommandType.END_GAME,
            GameSessionStatus.RUNNING,
            GamePhase.DISCUSSION,
            OwnershipIntentType.RELEASE,
        ),
    ],
)
def test_ownership_intent_is_command_scoped(
    command_type: SessionCommandType,
    status: GameSessionStatus,
    phase: GamePhase,
    expected_intent: OwnershipIntentType,
) -> None:
    context = make_context(
        command_type,
        status=status,
        current_phase=phase,
    )

    outcome = LifecyclePhaseControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildPlanReady)
    assert outcome.plan.ownership_intent.intent_type is expected_intent
