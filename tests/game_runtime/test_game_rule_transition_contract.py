from __future__ import annotations

import ast
from dataclasses import fields, is_dataclass, replace
from pathlib import Path

import pytest

from game_runtime.session_control import (
    GameRuleActivationEffect,
    GameRuleActivationRejectReason,
    GameRuleActivationState,
    GameRuleActivationAccepted,
    GameRuleActivationContractError,
    GameRuleActivationContractFailureReason,
    GameRuleActivationRejected,
    GameRuleActivationRequest,
    transition_game_rule_activation,
)


EMPTY = GameRuleActivationState(None, None)
REQUESTED = GameRuleActivationState("rule-set:commit-1", "hidden:commit-1")
OTHER = GameRuleActivationState("rule-set:commit-2", "hidden:commit-2")


def test_empty_state_accepts_a_complete_activation_request() -> None:
    decision = transition_game_rule_activation(
        GameRuleActivationRequest(EMPTY, REQUESTED)
    )

    assert isinstance(decision, GameRuleActivationAccepted)
    assert decision.previous_state is EMPTY
    assert decision.resulting_state is REQUESTED
    assert decision.effect is GameRuleActivationEffect.ACTIVATE_RULE_SET


def test_active_equal_state_is_rejected_as_already_active() -> None:
    decision = transition_game_rule_activation(
        GameRuleActivationRequest(REQUESTED, REQUESTED)
    )

    assert isinstance(decision, GameRuleActivationRejected)
    assert decision.reason is GameRuleActivationRejectReason.RULE_SET_ALREADY_ACTIVE


def test_active_different_state_is_rejected_as_conflict() -> None:
    decision = transition_game_rule_activation(
        GameRuleActivationRequest(REQUESTED, OTHER)
    )

    assert isinstance(decision, GameRuleActivationRejected)
    assert decision.reason is GameRuleActivationRejectReason.RULE_SET_CONFLICT


@pytest.mark.parametrize(
    ("rule_reference", "hidden_reference", "reason"),
    [
        ("rule-set:commit-1", None, GameRuleActivationContractFailureReason.INCOMPLETE_STATE),
        (None, "hidden:commit-1", GameRuleActivationContractFailureReason.INCOMPLETE_STATE),
        (" ", "hidden:commit-1", GameRuleActivationContractFailureReason.INVALID_STATE_VALUE),
        ("rule-set:commit-1", "\t", GameRuleActivationContractFailureReason.INVALID_STATE_VALUE),
    ],
)
def test_state_rejects_partial_and_malformed_values(
    rule_reference: str | None,
    hidden_reference: str | None,
    reason: GameRuleActivationContractFailureReason,
) -> None:
    with pytest.raises(GameRuleActivationContractError) as error:
        GameRuleActivationState(rule_reference, hidden_reference)

    assert error.value.reason is reason


@pytest.mark.parametrize(
    "kwargs",
    [
        {"committed_rule_set_reference": 1, "opaque_hidden_state_reference": "hidden:commit-1"},
        {"committed_rule_set_reference": "rule-set:commit-1", "opaque_hidden_state_reference": 1},
    ],
)
def test_state_rejects_wrong_value_types(kwargs: dict[str, object]) -> None:
    with pytest.raises(TypeError):
        GameRuleActivationState(**kwargs)  # type: ignore[arg-type]


def test_request_rejects_empty_requested_state_and_wrong_state_types() -> None:
    with pytest.raises(GameRuleActivationContractError) as error:
        GameRuleActivationRequest(REQUESTED, EMPTY)
    assert error.value.reason is GameRuleActivationContractFailureReason.EMPTY_REQUESTED_STATE

    with pytest.raises(TypeError):
        GameRuleActivationRequest("invalid", REQUESTED)  # type: ignore[arg-type]


def test_transition_rejects_wrong_request_type_and_is_deterministic() -> None:
    with pytest.raises(TypeError, match="GameRuleActivationRequest"):
        transition_game_rule_activation("invalid")  # type: ignore[arg-type]

    request = GameRuleActivationRequest(REQUESTED, OTHER)
    assert transition_game_rule_activation(request) == transition_game_rule_activation(request)


def test_transition_contract_values_are_frozen_slotted_and_closed() -> None:
    values = (
        EMPTY,
        GameRuleActivationRequest(EMPTY, REQUESTED),
        GameRuleActivationAccepted(
            EMPTY, REQUESTED, GameRuleActivationEffect.ACTIVATE_RULE_SET
        ),
        GameRuleActivationRejected(
            REQUESTED,
            OTHER,
            GameRuleActivationRejectReason.RULE_SET_CONFLICT,
        ),
    )
    for value in values:
        assert is_dataclass(value)
        assert hasattr(type(value), "__slots__")
        with pytest.raises((AttributeError, TypeError)):
            setattr(value, fields(value)[0].name, "changed")

    with pytest.raises((AttributeError, TypeError)):
        replace(
            values[-1], reason="RULE_SET_CONFLICT"  # type: ignore[arg-type]
        )


def test_transition_module_has_no_runtime_or_mutable_dependencies() -> None:
    source = Path("game_runtime/session_control/game_rule_transition.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    forbidden = ("actor", "clock", "random", "repository", "persistence", "io")
    assert not any(token in module.lower() for module in imported_modules for token in forbidden)
    assert not any(
        isinstance(node.value, (ast.Dict, ast.List, ast.Set))
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
    )
