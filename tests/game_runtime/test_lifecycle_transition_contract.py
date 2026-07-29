from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, fields
import inspect

import pytest

from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control import (
    LifecycleTransitionAccepted,
    LifecycleTransitionContractError,
    LifecycleTransitionPhaseEffect,
    LifecycleTransitionRejectReason,
    LifecycleTransitionRejected,
    LifecycleTransitionRequest,
    LifecycleTransitionState,
    LifecycleControlApplyPlanBuilder,
    SessionCommandType,
    transition_lifecycle,
)


def request(
    operation: SessionCommandType,
    status: GameSessionStatus,
    phase: GamePhase,
) -> LifecycleTransitionRequest:
    return LifecycleTransitionRequest(
        operation=operation,
        current_state=LifecycleTransitionState(
            lifecycle_status=status,
            phase=phase,
        ),
    )


def test_transition_contracts_are_frozen_slotted_values() -> None:
    transition_request = request(
        SessionCommandType.START_GAME,
        GameSessionStatus.CREATED,
        GamePhase.LOBBY,
    )
    accepted = transition_lifecycle(transition_request)
    rejected = transition_lifecycle(
        request(
            SessionCommandType.PAUSE_GAME,
            GameSessionStatus.CREATED,
            GamePhase.LOBBY,
        )
    )
    values = (
        transition_request.current_state,
        transition_request,
        accepted,
        rejected,
    )
    forbidden = (
        "actor",
        "session ",
        "snapshot",
        "receipt",
        "claim",
        "port",
        "cursor",
        "version",
        "event",
        "callback",
        "task",
    )

    for value in values:
        assert not hasattr(value, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(value, fields(value)[0].name, None)
        for field in fields(value):
            contract = f"{field.name} {field.type}".lower()
            assert not any(term in contract for term in forbidden)


def test_start_transitions_created_lobby_to_running_introduction() -> None:
    current = LifecycleTransitionState(
        lifecycle_status=GameSessionStatus.CREATED,
        phase=GamePhase.LOBBY,
    )

    decision = transition_lifecycle(
        LifecycleTransitionRequest(
            operation=SessionCommandType.START_GAME,
            current_state=current,
        )
    )

    assert isinstance(decision, LifecycleTransitionAccepted)
    assert decision.operation is SessionCommandType.START_GAME
    assert decision.previous_state is current
    assert decision.resulting_state == LifecycleTransitionState(
        lifecycle_status=GameSessionStatus.RUNNING,
        phase=GamePhase.INTRODUCTION,
    )
    assert decision.phase_effect is LifecycleTransitionPhaseEffect.START_BOOTSTRAP


@pytest.mark.parametrize(
    ("status", "phase"),
    [
        (GameSessionStatus.RUNNING, GamePhase.EXPLORATION),
        (GameSessionStatus.PAUSED, GamePhase.DISCUSSION),
        (GameSessionStatus.ENDED, GamePhase.ENDING),
    ],
)
def test_start_rejects_other_lifecycle_states(
    status: GameSessionStatus,
    phase: GamePhase,
) -> None:
    decision = transition_lifecycle(
        request(SessionCommandType.START_GAME, status, phase)
    )

    assert isinstance(decision, LifecycleTransitionRejected)
    assert decision.reason is LifecycleTransitionRejectReason.INVALID_LIFECYCLE_TRANSITION


@pytest.mark.parametrize(
    "phase",
    [
        GamePhase.INTRODUCTION,
        GamePhase.EXPLORATION,
        GamePhase.DISCUSSION,
        GamePhase.VOTING,
        GamePhase.ENDING,
    ],
)
def test_pause_transitions_running_to_paused_without_changing_phase(
    phase: GamePhase,
) -> None:
    current = LifecycleTransitionState(
        lifecycle_status=GameSessionStatus.RUNNING,
        phase=phase,
    )

    decision = transition_lifecycle(
        LifecycleTransitionRequest(
            operation=SessionCommandType.PAUSE_GAME,
            current_state=current,
        )
    )

    assert isinstance(decision, LifecycleTransitionAccepted)
    assert decision.previous_state is current
    assert decision.resulting_state == LifecycleTransitionState(
        lifecycle_status=GameSessionStatus.PAUSED,
        phase=phase,
    )
    assert decision.phase_effect is LifecycleTransitionPhaseEffect.UNCHANGED


@pytest.mark.parametrize(
    ("status", "phase"),
    [
        (GameSessionStatus.CREATED, GamePhase.LOBBY),
        (GameSessionStatus.PAUSED, GamePhase.DISCUSSION),
        (GameSessionStatus.ENDED, GamePhase.ENDING),
    ],
)
def test_pause_rejects_non_running_states(
    status: GameSessionStatus,
    phase: GamePhase,
) -> None:
    decision = transition_lifecycle(
        request(SessionCommandType.PAUSE_GAME, status, phase)
    )

    assert isinstance(decision, LifecycleTransitionRejected)
    assert decision.reason is LifecycleTransitionRejectReason.INVALID_LIFECYCLE_TRANSITION


@pytest.mark.parametrize(
    ("status", "phase"),
    [
        (GameSessionStatus.CREATED, GamePhase.LOBBY),
        (GameSessionStatus.RUNNING, GamePhase.EXPLORATION),
        (GameSessionStatus.PAUSED, GamePhase.DISCUSSION),
    ],
)
def test_end_transitions_allowed_states_to_ended_ending(
    status: GameSessionStatus,
    phase: GamePhase,
) -> None:
    current = LifecycleTransitionState(lifecycle_status=status, phase=phase)

    decision = transition_lifecycle(
        LifecycleTransitionRequest(
            operation=SessionCommandType.END_GAME,
            current_state=current,
        )
    )

    assert isinstance(decision, LifecycleTransitionAccepted)
    assert decision.previous_state is current
    assert decision.resulting_state == LifecycleTransitionState(
        lifecycle_status=GameSessionStatus.ENDED,
        phase=GamePhase.ENDING,
    )
    assert decision.phase_effect is LifecycleTransitionPhaseEffect.END_TERMINAL


def test_repeated_end_is_a_typed_domain_rejection() -> None:
    decision = transition_lifecycle(
        request(
            SessionCommandType.END_GAME,
            GameSessionStatus.ENDED,
            GamePhase.ENDING,
        )
    )

    assert isinstance(decision, LifecycleTransitionRejected)
    assert decision.reason is LifecycleTransitionRejectReason.SESSION_ALREADY_ENDED


@pytest.mark.parametrize(
    ("status", "phase"),
    [
        (GameSessionStatus.CREATED, GamePhase.INTRODUCTION),
        (GameSessionStatus.RUNNING, GamePhase.LOBBY),
        (GameSessionStatus.PAUSED, GamePhase.LOBBY),
        (GameSessionStatus.ENDED, GamePhase.DISCUSSION),
    ],
)
def test_invalid_state_pairs_fail_closed_at_contract_boundary(
    status: GameSessionStatus,
    phase: GamePhase,
) -> None:
    with pytest.raises(LifecycleTransitionContractError):
        LifecycleTransitionState(lifecycle_status=status, phase=phase)


def test_malformed_or_unsupported_request_fails_closed() -> None:
    with pytest.raises(LifecycleTransitionContractError):
        LifecycleTransitionRequest(
            operation=SessionCommandType.CHANGE_PHASE,
            current_state=LifecycleTransitionState(
                lifecycle_status=GameSessionStatus.RUNNING,
                phase=GamePhase.EXPLORATION,
            ),
        )
    with pytest.raises(LifecycleTransitionContractError):
        transition_lifecycle(object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("operation", "status", "phase"),
    [
        (SessionCommandType.START_GAME, GameSessionStatus.CREATED, GamePhase.LOBBY),
        (
            SessionCommandType.PAUSE_GAME,
            GameSessionStatus.RUNNING,
            GamePhase.EXPLORATION,
        ),
        (SessionCommandType.END_GAME, GameSessionStatus.PAUSED, GamePhase.DISCUSSION),
        (SessionCommandType.END_GAME, GameSessionStatus.ENDED, GamePhase.ENDING),
    ],
)
def test_transition_is_deterministic_without_input_mutation(
    operation: SessionCommandType,
    status: GameSessionStatus,
    phase: GamePhase,
) -> None:
    transition_request = request(operation, status, phase)
    before = transition_request.current_state

    first = transition_lifecycle(transition_request)
    second = transition_lifecycle(transition_request)

    assert first == second
    assert first is not second
    assert transition_request.current_state is before


def test_transition_function_has_no_forbidden_dependencies() -> None:
    module = inspect.getmodule(transition_lifecycle)
    assert module is not None
    tree = ast.parse(inspect.getsource(module))
    imports = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert not inspect.iscoroutinefunction(transition_lifecycle)
    assert not any(
        forbidden in imported
        for imported in imports
        for forbidden in (
            "actor",
            "apply_contract",
            "event",
            "persistence",
            "recovery",
            "receipt",
            "coordinator",
            "builder",
            "random",
            "uuid",
        )
    )
    assert calls.isdisjoint(
        {
            "now",
            "utcnow",
            "uuid4",
            "transition_to",
            "transition_phase_to",
            "append",
            "commit",
            "apply",
            "reduce",
        }
    )


def test_lifecycle_builder_routes_domain_state_through_transition_function() -> None:
    module = inspect.getmodule(LifecycleControlApplyPlanBuilder)
    assert module is not None
    tree = ast.parse(inspect.getsource(module))
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "transition_lifecycle" in imported_names
    assert "transition_lifecycle" in called_names
