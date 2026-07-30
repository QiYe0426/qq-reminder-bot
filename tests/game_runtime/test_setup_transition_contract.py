from __future__ import annotations

import ast
from dataclasses import fields, is_dataclass, replace
from pathlib import Path

import pytest

from game_runtime.session_control import (
    SetupTransitionAccepted,
    SetupTransitionDecision,
    SetupTransitionContractError,
    SetupTransitionContractFailureReason,
    SetupTransitionEffect,
    SetupTransitionRejectReason,
    SetupTransitionRejected,
    SetupTransitionRequest,
    SetupTransitionState,
    transition_setup,
)


EMPTY = SetupTransitionState(script_id=None, manifest_reference=None)
SCRIPT_ONE = SetupTransitionState(
    script_id="script-1", manifest_reference="manifest-1"
)
SCRIPT_TWO = SetupTransitionState(
    script_id="script-2", manifest_reference="manifest-2"
)


def test_empty_setup_initializes_requested_script() -> None:
    decision = transition_setup(
        SetupTransitionRequest(current_state=EMPTY, requested_state=SCRIPT_ONE)
    )

    assert isinstance(decision, SetupTransitionAccepted)
    assert decision.previous_state is EMPTY
    assert decision.resulting_state is SCRIPT_ONE
    assert decision.effect is SetupTransitionEffect.INITIALIZE


def test_complete_different_setup_replaces_script() -> None:
    decision = transition_setup(
        SetupTransitionRequest(current_state=SCRIPT_ONE, requested_state=SCRIPT_TWO)
    )

    assert isinstance(decision, SetupTransitionAccepted)
    assert decision.previous_state is SCRIPT_ONE
    assert decision.resulting_state is SCRIPT_TWO
    assert decision.effect is SetupTransitionEffect.REPLACE


def test_identical_setup_is_rejected_as_already_set() -> None:
    decision = transition_setup(
        SetupTransitionRequest(current_state=SCRIPT_ONE, requested_state=SCRIPT_ONE)
    )

    assert isinstance(decision, SetupTransitionRejected)
    assert decision.current_state is SCRIPT_ONE
    assert decision.requested_state is SCRIPT_ONE
    assert decision.reason is SetupTransitionRejectReason.SCRIPT_ALREADY_SET


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        (
            {"script_id": "script-1", "manifest_reference": None},
            SetupTransitionContractFailureReason.INCOMPLETE_STATE,
        ),
        (
            {"script_id": None, "manifest_reference": "manifest-1"},
            SetupTransitionContractFailureReason.INCOMPLETE_STATE,
        ),
        (
            {"script_id": " ", "manifest_reference": "manifest-1"},
            SetupTransitionContractFailureReason.INVALID_STATE_VALUE,
        ),
        (
            {"script_id": "script-1", "manifest_reference": "\t"},
            SetupTransitionContractFailureReason.INVALID_STATE_VALUE,
        ),
    ],
)
def test_state_rejects_malformed_value_combinations(
    kwargs: dict[str, str | None],
    reason: SetupTransitionContractFailureReason,
) -> None:
    with pytest.raises(SetupTransitionContractError) as error:
        SetupTransitionState(**kwargs)

    assert error.value.reason is reason


@pytest.mark.parametrize(
    "kwargs",
    [
        {"script_id": 1, "manifest_reference": "manifest-1"},
        {"script_id": "script-1", "manifest_reference": 1},
    ],
)
def test_state_rejects_wrong_value_types(kwargs: dict[str, object]) -> None:
    with pytest.raises(TypeError):
        SetupTransitionState(**kwargs)  # type: ignore[arg-type]


def test_request_rejects_empty_requested_state() -> None:
    with pytest.raises(SetupTransitionContractError) as error:
        SetupTransitionRequest(current_state=SCRIPT_ONE, requested_state=EMPTY)

    assert error.value.reason is SetupTransitionContractFailureReason.EMPTY_REQUESTED_STATE


@pytest.mark.parametrize(
    "kwargs",
    [
        {"current_state": "not-a-state", "requested_state": SCRIPT_ONE},
        {"current_state": SCRIPT_ONE, "requested_state": "not-a-state"},
    ],
)
def test_request_rejects_wrong_state_types(kwargs: dict[str, object]) -> None:
    with pytest.raises(TypeError):
        SetupTransitionRequest(**kwargs)  # type: ignore[arg-type]


def test_transition_rejects_wrong_request_type() -> None:
    with pytest.raises(TypeError, match="SetupTransitionRequest"):
        transition_setup("not-a-request")  # type: ignore[arg-type]


def test_decision_values_are_immutable_and_slotted() -> None:
    values = (
        EMPTY,
        SCRIPT_ONE,
        SetupTransitionRequest(current_state=EMPTY, requested_state=SCRIPT_ONE),
        SetupTransitionAccepted(
            previous_state=EMPTY,
            resulting_state=SCRIPT_ONE,
            effect=SetupTransitionEffect.INITIALIZE,
        ),
        SetupTransitionRejected(
            current_state=SCRIPT_ONE,
            requested_state=SCRIPT_ONE,
            reason=SetupTransitionRejectReason.SCRIPT_ALREADY_SET,
        ),
    )

    for value in values:
        assert is_dataclass(value)
        assert hasattr(type(value), "__slots__")
        with pytest.raises((AttributeError, TypeError)):
            setattr(value, fields(value)[0].name, "mutated")


def test_decision_alias_and_transition_are_deterministic() -> None:
    request = SetupTransitionRequest(current_state=SCRIPT_ONE, requested_state=SCRIPT_TWO)
    first: SetupTransitionDecision = transition_setup(request)
    second: SetupTransitionDecision = transition_setup(request)

    assert first == second
    assert isinstance(first, SetupTransitionAccepted | SetupTransitionRejected)


def test_public_exports_are_closed_and_transition_has_no_runtime_dependencies() -> None:
    import game_runtime.session_control as session_control

    exported = {
        "SetupTransitionAccepted",
        "SetupTransitionDecision",
        "SetupTransitionContractError",
        "SetupTransitionContractFailureReason",
        "SetupTransitionEffect",
        "SetupTransitionRejectReason",
        "SetupTransitionRejected",
        "SetupTransitionRequest",
        "SetupTransitionState",
        "transition_setup",
    }
    assert exported <= set(session_control.__all__)
    assert all(getattr(session_control, name) is globals()[name] for name in exported)
    assert not {
        "Accepted",
        "Decision",
        "RejectReason",
        "Rejected",
    } & set(session_control.__all__)
    assert not any(
        hasattr(session_control, name)
        for name in ("Accepted", "Decision", "RejectReason", "Rejected")
    )

    source = Path("game_runtime/session_control/setup_transition.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    imported_modules = {
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
        for name in (node.module,)
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    forbidden = ("actor", "clock", "random", "repository", "persistence", "io")
    assert not any(
        token in module.lower() for module in imported_modules for token in forbidden
    )

    mutable_global_values = [
        node.value
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and isinstance(node.value, (ast.Dict, ast.List, ast.Set))
    ]
    assert not mutable_global_values


def test_result_values_reject_wrong_contract_types() -> None:
    with pytest.raises(TypeError):
        SetupTransitionAccepted(  # type: ignore[arg-type]
            previous_state="not-a-state",
            resulting_state=SCRIPT_ONE,
            effect=SetupTransitionEffect.INITIALIZE,
        )
    with pytest.raises(TypeError):
        SetupTransitionRejected(  # type: ignore[arg-type]
            current_state=SCRIPT_ONE,
            requested_state=SCRIPT_ONE,
            reason="SCRIPT_ALREADY_SET",
        )
    with pytest.raises(TypeError):
        SetupTransitionContractError("INVALID_STATE_VALUE")  # type: ignore[arg-type]


def test_rejected_values_do_not_admit_modified_identity() -> None:
    rejected = SetupTransitionRejected(
        current_state=SCRIPT_ONE,
        requested_state=SCRIPT_ONE,
        reason=SetupTransitionRejectReason.SCRIPT_ALREADY_SET,
    )

    with pytest.raises((AttributeError, TypeError)):
        replace(rejected, reason="SCRIPT_ALREADY_SET")  # type: ignore[arg-type]
