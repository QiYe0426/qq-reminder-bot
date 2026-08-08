from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import ast
import inspect

import pytest

import game_runtime.session_control as session_control


def _contract(name: str):
    value = getattr(session_control, name, None)
    assert value is not None, f"missing contract: {name}"
    return value


def _empty():
    return _contract("QuestActivationState")(None, None, None)


def _active(quest_id: str = "quest-1"):
    return _contract("QuestActivationState")(
        quest_id,
        "rule-set:commit-1",
        f"quest-public:{quest_id}",
    )


def test_available_quest_activation_is_pure_and_deterministic() -> None:
    request = _contract("QuestActivationRequest")(
        quest_id="quest-1",
        current_state=_empty(),
        requested_state=_active(),
        disposition=session_control.QuestActivationDisposition.AVAILABLE,
    )

    first = _contract("transition_quest_activation")(request)
    second = _contract("transition_quest_activation")(request)

    assert first == second
    assert isinstance(first, _contract("QuestActivationAccepted"))
    assert first.previous_state == request.current_state
    assert first.resulting_state == request.requested_state
    assert first.effect is _contract("QuestActivationEffect").ACTIVATE
    assert not hasattr(first, "__dict__")
    with pytest.raises(FrozenInstanceError):
        first.quest_id = "quest-other"


@pytest.mark.parametrize(
    ("disposition_name", "current_quest_id", "reason_name"),
    [
        ("ALREADY_ACTIVE", "quest-1", "QUEST_ALREADY_ACTIVE"),
        ("CONFLICT", "quest-other", "QUEST_CONFLICT"),
        ("NOT_FOUND", None, "QUEST_NOT_FOUND"),
        ("NOT_ACTIVATABLE", None, "QUEST_NOT_ACTIVATABLE"),
        ("RULE_SET_NOT_ACTIVE", None, "RULE_SET_NOT_ACTIVE"),
    ],
)
def test_business_dispositions_map_to_typed_rejections(
    disposition_name: str,
    current_quest_id: str | None,
    reason_name: str,
) -> None:
    request = _contract("QuestActivationRequest")(
        quest_id="quest-1",
        current_state=(
            _empty() if current_quest_id is None else _active(current_quest_id)
        ),
        requested_state=None,
        disposition=getattr(
            session_control.QuestActivationDisposition,
            disposition_name,
        ),
    )

    decision = _contract("transition_quest_activation")(request)

    assert isinstance(decision, _contract("QuestActivationRejected"))
    assert decision.reason is getattr(
        _contract("QuestActivationRejectReason"),
        reason_name,
    )


@pytest.mark.parametrize(
    "values",
    [
        ("quest-1", None, "quest-public:1"),
        (None, "rule-set:1", None),
        (None, None, "quest-public:1"),
    ],
)
def test_partial_state_is_rejected(values: tuple[object, object, object]) -> None:
    with pytest.raises(ValueError):
        _contract("QuestActivationState")(*values)


def test_unknown_or_contradictory_request_fails_closed() -> None:
    request_type = _contract("QuestActivationRequest")
    with pytest.raises(ValueError):
        request_type(
            quest_id="quest-1",
            current_state=_empty(),
            requested_state=None,
            disposition=session_control.QuestActivationDisposition.UNKNOWN,
        )
    with pytest.raises(ValueError):
        request_type(
            quest_id="quest-1",
            current_state=_active("quest-other"),
            requested_state=None,
            disposition=session_control.QuestActivationDisposition.ALREADY_ACTIVE,
        )


def test_transition_outputs_reject_malformed_external_construction() -> None:
    request = _contract("QuestActivationRequest")(
        quest_id="quest-1",
        current_state=_empty(),
        requested_state=_active(),
        disposition=session_control.QuestActivationDisposition.AVAILABLE,
    )
    accepted = _contract("transition_quest_activation")(request)

    with pytest.raises(TypeError):
        replace(accepted, effect=object())
    with pytest.raises(ValueError):
        replace(accepted, quest_id="quest-other")
    rejected_type = _contract("QuestActivationRejected")
    with pytest.raises(TypeError):
        rejected_type("quest-1", _empty(), object())


def test_transition_module_has_no_runtime_or_nondeterministic_dependencies() -> None:
    module = inspect.getmodule(_contract("transition_quest_activation"))
    assert module is not None
    tree = ast.parse(inspect.getsource(module))
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
    forbidden = {
        "actor",
        "coordinator",
        "persistence",
        "event_store",
        "recovery",
        "notification",
        "asyncio",
        "random",
        "uuid",
        "plugin",
        "tool",
        "llm",
    }
    assert not any(
        token in imported.casefold()
        for imported in imports
        for token in forbidden
    )
