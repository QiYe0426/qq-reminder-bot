from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from game_runtime.session import GamePhase
from game_runtime.session_control import phase_transition as phase_transition_module
from game_runtime.session_control.phase_transition import (
    PhaseTransitionAccepted,
    PhaseTransitionRejectReason,
    PhaseTransitionRejected,
    PhaseTransitionRequest,
    transition_phase,
)


_LEGAL_EDGES = frozenset(
    {
        (GamePhase.INTRODUCTION, GamePhase.EXPLORATION),
        (GamePhase.EXPLORATION, GamePhase.DISCUSSION),
        (GamePhase.DISCUSSION, GamePhase.EXPLORATION),
        (GamePhase.DISCUSSION, GamePhase.VOTING),
        (GamePhase.VOTING, GamePhase.DISCUSSION),
        (GamePhase.VOTING, GamePhase.ENDING),
    }
)


def test_authoritative_phase_graph_is_one_immutable_complete_edge_set() -> None:
    authority = getattr(phase_transition_module, "_LEGAL_EDGES", None)
    private_mutable_dicts = {
        name
        for name, value in vars(phase_transition_module).items()
        if name.startswith("_")
        and not name.startswith("__")
        and isinstance(value, dict)
    }

    assert isinstance(authority, frozenset)
    assert authority == _LEGAL_EDGES
    assert private_mutable_dicts == set()
    edge = (GamePhase.INTRODUCTION, GamePhase.EXPLORATION)
    with pytest.raises(TypeError):
        authority[edge] = edge  # type: ignore[index]
    with pytest.raises(TypeError):
        del authority[edge]  # type: ignore[index]
    with pytest.raises(AttributeError):
        authority.clear()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("current_phase", "target_phase"),
    [
        (current_phase, target_phase)
        for current_phase in GamePhase
        for target_phase in GamePhase
        if (current_phase, target_phase) in _LEGAL_EDGES
    ],
)
def test_transition_phase_accepts_every_frozen_legal_edge(
    current_phase: GamePhase,
    target_phase: GamePhase,
) -> None:
    decision = transition_phase(
        PhaseTransitionRequest(current_phase=current_phase, target_phase=target_phase)
    )

    assert decision == PhaseTransitionAccepted(
        previous_phase=current_phase,
        resulting_phase=target_phase,
    )


@pytest.mark.parametrize(
    ("current_phase", "target_phase"),
    [
        (current_phase, target_phase)
        for current_phase in GamePhase
        for target_phase in GamePhase
        if (current_phase, target_phase) not in _LEGAL_EDGES
    ],
)
def test_transition_phase_rejects_every_non_edge(
    current_phase: GamePhase,
    target_phase: GamePhase,
) -> None:
    decision = transition_phase(
        PhaseTransitionRequest(current_phase=current_phase, target_phase=target_phase)
    )

    assert decision == PhaseTransitionRejected(
        current_phase=current_phase,
        requested_phase=target_phase,
        reason=PhaseTransitionRejectReason.INVALID_PHASE_TRANSITION,
    )


def test_transition_contract_values_are_immutable_slot_based_values() -> None:
    request = PhaseTransitionRequest(
        current_phase=GamePhase.INTRODUCTION,
        target_phase=GamePhase.EXPLORATION,
    )
    accepted = PhaseTransitionAccepted(
        previous_phase=GamePhase.INTRODUCTION,
        resulting_phase=GamePhase.EXPLORATION,
    )
    rejected = PhaseTransitionRejected(
        current_phase=GamePhase.LOBBY,
        requested_phase=GamePhase.INTRODUCTION,
        reason=PhaseTransitionRejectReason.INVALID_PHASE_TRANSITION,
    )

    for value, attribute in (
        (request, "current_phase"),
        (accepted, "previous_phase"),
        (rejected, "current_phase"),
    ):
        assert hasattr(type(value), "__slots__")
        assert not hasattr(value, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(value, attribute, GamePhase.LOBBY)


def test_transition_phase_is_pure_and_deterministic() -> None:
    request = PhaseTransitionRequest(
        current_phase=GamePhase.DISCUSSION,
        target_phase=GamePhase.VOTING,
    )

    assert transition_phase(request) == transition_phase(request)
    assert transition_phase(request) == PhaseTransitionAccepted(
        previous_phase=GamePhase.DISCUSSION,
        resulting_phase=GamePhase.VOTING,
    )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: PhaseTransitionRequest(
            current_phase="INTRODUCTION",  # type: ignore[arg-type]
            target_phase=GamePhase.EXPLORATION,
        ),
        lambda: PhaseTransitionRequest(
            current_phase=GamePhase.INTRODUCTION,
            target_phase="EXPLORATION",  # type: ignore[arg-type]
        ),
        lambda: PhaseTransitionAccepted(
            previous_phase="INTRODUCTION",  # type: ignore[arg-type]
            resulting_phase=GamePhase.EXPLORATION,
        ),
        lambda: PhaseTransitionRejected(
            current_phase=GamePhase.LOBBY,
            requested_phase=GamePhase.INTRODUCTION,
            reason="INVALID_PHASE_TRANSITION",  # type: ignore[arg-type]
        ),
        lambda: transition_phase(object()),  # type: ignore[arg-type]
    ],
)
def test_transition_phase_rejects_malformed_types(factory: object) -> None:
    assert callable(factory)
    with pytest.raises(TypeError):
        factory()


def test_phase_transition_contract_is_publicly_exported() -> None:
    from game_runtime.session_control import (
        PhaseTransitionAccepted as PublicAccepted,
    )
    from game_runtime.session_control import (
        PhaseTransitionRejectReason as PublicRejectReason,
    )
    from game_runtime.session_control import (
        PhaseTransitionRejected as PublicRejected,
    )
    from game_runtime.session_control import (
        PhaseTransitionRequest as PublicRequest,
    )
    from game_runtime.session_control import transition_phase as public_transition

    assert PublicRequest is PhaseTransitionRequest
    assert PublicAccepted is PhaseTransitionAccepted
    assert PublicRejected is PhaseTransitionRejected
    assert PublicRejectReason is PhaseTransitionRejectReason
    assert public_transition is transition_phase


def test_phase_transition_module_has_no_forbidden_control_dependencies() -> None:
    module_path = Path(__file__).parents[2] / "game_runtime" / "session_control" / "phase_transition.py"
    module = ast.parse(module_path.read_text(encoding="utf-8"))
    forbidden_terms = {
        "actor",
        "gate",
        "processor",
        "coordinator",
        "persistence",
        "event_store",
        "eventstore",
        "recovery",
        "notification",
        "plugin",
        "tool",
        "llm",
    }
    imports = {
        node.module
        for node in ast.walk(module)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    } | {
        alias.name
        for node in ast.walk(module)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    assert all(
        not any(term in module_name.casefold() for term in forbidden_terms)
        for module_name in imports
    )
