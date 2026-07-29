from __future__ import annotations

import ast
from dataclasses import replace
import inspect

import pytest

from game_runtime.event import GameEventType
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control import (
    BuildNonCommit,
    BuildPlanReady,
    BuildReject,
    ControlLifecycleApplyEvidence,
    LifecycleControlApplyPlanBuilder,
    LifecycleTransitionAccepted,
    LifecycleTransitionRequest,
    LifecycleTransitionState,
    OwnershipIntent,
    OwnershipIntentType,
    SessionCommandType,
    transition_lifecycle,
)
from game_runtime.session_control.lifecycle_phase_builder import (
    _compose_lifecycle_apply_plan,
)
import game_runtime.session_control.lifecycle_phase_builder as builder_module
from test_lifecycle_phase_apply_plan_builder import make_context


def build_success(
    command_type: SessionCommandType,
    *,
    status: GameSessionStatus,
    phase: GamePhase,
    active_generation: int | None | object = object(),
):
    context = make_context(
        command_type,
        status=status,
        current_phase=phase,
        active_generation=active_generation,
    )
    outcome = LifecycleControlApplyPlanBuilder().build(context)
    assert isinstance(outcome, BuildPlanReady)
    return context, outcome.plan


def accepted_transition(context) -> LifecycleTransitionAccepted:
    decision = transition_lifecycle(
        LifecycleTransitionRequest(
            operation=context.command_intent.command_type,
            current_state=LifecycleTransitionState(
                lifecycle_status=context.session_view.status,
                phase=context.session_view.current_phase,
            ),
        )
    )
    assert isinstance(decision, LifecycleTransitionAccepted)
    return decision


def test_composition_helper_is_private_sync_stateless_and_has_no_io() -> None:
    assert _compose_lifecycle_apply_plan.__name__.startswith("_")
    assert not inspect.iscoroutinefunction(_compose_lifecycle_apply_plan)
    assert not hasattr(_compose_lifecycle_apply_plan, "__dict__") or not vars(
        _compose_lifecycle_apply_plan
    )
    tree = ast.parse(inspect.getsource(_compose_lifecycle_apply_plan))
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

    assert called_names.isdisjoint({"open", "print", "input"})
    assert called_attributes.isdisjoint(
        {
            "coordinate",
            "commit_control_apply",
            "commit_control_rejection",
            "append_event",
            "transition_to",
            "transition_phase_to",
            "send",
        }
    )


@pytest.mark.parametrize(
    ("command_type", "status", "phase", "expected_status", "expected_phase"),
    [
        (
            SessionCommandType.START_GAME,
            GameSessionStatus.CREATED,
            GamePhase.LOBBY,
            GameSessionStatus.RUNNING,
            GamePhase.INTRODUCTION,
        ),
        (
            SessionCommandType.PAUSE_GAME,
            GameSessionStatus.RUNNING,
            GamePhase.EXPLORATION,
            GameSessionStatus.PAUSED,
            GamePhase.EXPLORATION,
        ),
        (
            SessionCommandType.END_GAME,
            GameSessionStatus.RUNNING,
            GamePhase.DISCUSSION,
            GameSessionStatus.ENDED,
            GamePhase.ENDING,
        ),
    ],
)
def test_candidate_is_bound_to_session_identity_transition_version_and_cursor(
    command_type: SessionCommandType,
    status: GameSessionStatus,
    phase: GamePhase,
    expected_status: GameSessionStatus,
    expected_phase: GamePhase,
) -> None:
    context, plan = build_success(command_type, status=status, phase=phase)
    candidate = plan.candidate_snapshot

    assert (
        candidate.game_id,
        candidate.session_id,
        candidate.group_id,
        candidate.dm_participant_id,
    ) == (
        context.session_view.game_id,
        context.session_view.session_id,
        context.session_view.group_id,
        context.session_view.dm_participant_id,
    )
    assert candidate.status is expected_status
    assert candidate.current_phase is expected_phase
    assert candidate.state_version == context.session_view.state_version + 1
    assert (
        candidate.last_applied_sequence_no
        == context.envelope.event_sequence_no
        == context.session_view.last_applied_sequence_no + 1
    )


@pytest.mark.parametrize(
    ("command_type", "status", "phase", "active_generation", "expected_intent"),
    [
        (
            SessionCommandType.START_GAME,
            GameSessionStatus.CREATED,
            GamePhase.LOBBY,
            None,
            OwnershipIntentType.ACQUIRE,
        ),
        (
            SessionCommandType.PAUSE_GAME,
            GameSessionStatus.RUNNING,
            GamePhase.EXPLORATION,
            3,
            OwnershipIntentType.RETAIN,
        ),
        (
            SessionCommandType.END_GAME,
            GameSessionStatus.CREATED,
            GamePhase.LOBBY,
            None,
            OwnershipIntentType.UNCHANGED,
        ),
        (
            SessionCommandType.END_GAME,
            GameSessionStatus.RUNNING,
            GamePhase.DISCUSSION,
            3,
            OwnershipIntentType.RELEASE,
        ),
        (
            SessionCommandType.END_GAME,
            GameSessionStatus.PAUSED,
            GamePhase.DISCUSSION,
            3,
            OwnershipIntentType.RELEASE,
        ),
    ],
)
def test_ownership_mapping_is_complete(
    command_type: SessionCommandType,
    status: GameSessionStatus,
    phase: GamePhase,
    active_generation: int | None,
    expected_intent: OwnershipIntentType,
) -> None:
    _, plan = build_success(
        command_type,
        status=status,
        phase=phase,
        active_generation=active_generation,
    )

    assert plan.ownership_intent.intent_type is expected_intent


@pytest.mark.parametrize(
    ("command_type", "status", "phase", "expected_events"),
    [
        (
            SessionCommandType.START_GAME,
            GameSessionStatus.CREATED,
            GamePhase.LOBBY,
            (GameEventType.SESSION_STARTED, GameEventType.PHASE_CHANGED),
        ),
        (
            SessionCommandType.PAUSE_GAME,
            GameSessionStatus.RUNNING,
            GamePhase.EXPLORATION,
            (GameEventType.SESSION_PAUSED,),
        ),
        (
            SessionCommandType.END_GAME,
            GameSessionStatus.RUNNING,
            GamePhase.DISCUSSION,
            (GameEventType.SESSION_ENDED,),
        ),
    ],
)
def test_result_event_type_order_identity_and_version_binding(
    command_type: SessionCommandType,
    status: GameSessionStatus,
    phase: GamePhase,
    expected_events: tuple[GameEventType, ...],
) -> None:
    context, plan = build_success(command_type, status=status, phase=phase)

    assert tuple(event.event_type for event in plan.result_events) == expected_events
    for ordinal, event in enumerate(plan.result_events, start=1):
        assert event.event_id == context.result_event_seed.derive_event_id(
            event.event_type,
            ordinal=ordinal,
        )
        assert event.game_id == plan.game_id
        assert event.session_id == plan.session_id
        assert event.causation_event_id == plan.input_event_id
        assert event.observed_state_version == plan.expected_state_version


def test_complete_plan_binds_all_required_components() -> None:
    context, plan = build_success(
        SessionCommandType.START_GAME,
        status=GameSessionStatus.CREATED,
        phase=GamePhase.LOBBY,
    )

    assert plan.candidate_snapshot is not None
    assert plan.ownership_intent is not None
    assert plan.lifecycle_evidence is context.lifecycle_evidence
    assert plan.command_id == context.envelope.command_id
    assert plan.operation_id == context.envelope.operation_id
    assert plan.operation_claim_id == context.claim.claim_id
    assert plan.input_event_id == context.envelope.event.event_id
    assert plan.expected_state_version == context.session_view.state_version
    assert plan.expected_cursor == context.session_view.last_applied_sequence_no
    assert plan.participant_mutations == ()
    assert plan.setup_mutations == ()


@pytest.mark.parametrize("invalid_part", ["transition", "ownership", "events"])
def test_invalid_composition_returns_noncommit_without_partial_plan(
    invalid_part: str,
) -> None:
    context, plan = build_success(
        SessionCommandType.START_GAME,
        status=GameSessionStatus.CREATED,
        phase=GamePhase.LOBBY,
    )
    transition = accepted_transition(context)
    ownership = plan.ownership_intent
    events = plan.result_events
    if invalid_part == "transition":
        transition = replace(
            transition,
            previous_state=LifecycleTransitionState(
                lifecycle_status=GameSessionStatus.PAUSED,
                phase=GamePhase.DISCUSSION,
            ),
        )
    elif invalid_part == "ownership":
        ownership = OwnershipIntent(
            intent_type=OwnershipIntentType.RETAIN,
            expected_generation=3,
            resulting_generation=3,
        )
    else:
        events = tuple(reversed(events))

    outcome = _compose_lifecycle_apply_plan(
        context=context,
        transition=transition,
        ownership_intent=ownership,
        result_events=events,
    )

    assert isinstance(outcome, BuildNonCommit)
    assert not hasattr(outcome, "plan")


def test_missing_evidence_returns_noncommit() -> None:
    context = make_context(
        SessionCommandType.START_GAME,
        status=GameSessionStatus.CREATED,
        current_phase=GamePhase.LOBBY,
        lifecycle_evidence=ControlLifecycleApplyEvidence(),
    )

    outcome = LifecycleControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildNonCommit)
    assert not hasattr(outcome, "plan")


def test_transition_rejection_bypasses_success_composition_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_composition(**_values: object):
        raise AssertionError("rejected transition must bypass success composition")

    monkeypatch.setattr(
        builder_module,
        "_compose_lifecycle_apply_plan",
        forbidden_composition,
    )
    context = make_context(
        SessionCommandType.PAUSE_GAME,
        status=GameSessionStatus.PAUSED,
        current_phase=GamePhase.EXPLORATION,
    )

    outcome = LifecycleControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildReject)
