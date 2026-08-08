from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import importlib
import importlib.util

import pytest

from game_runtime.session_control import ClueRevealDisposition


MODULE_NAME = "game_runtime.session_control.clue_reveal_transition"


def _module():
    return importlib.import_module(MODULE_NAME)


def _active_state(*, suffix: str = "1"):
    module = _module()
    return module.ClueRevealState(
        rule_set_reference="rule-set:commit-1",
        disclosure_state_reference=f"disclosure-state:commit-{suffix}",
        hidden_state_reference=f"hidden-state:commit-{suffix}",
    )


def test_transition_module_and_contracts_exist() -> None:
    assert importlib.util.find_spec(MODULE_NAME) is not None
    module = _module()
    for name in (
        "ClueRevealState",
        "ClueRevealRequest",
        "ClueRevealAccepted",
        "ClueRevealRejected",
        "ClueRevealEffect",
        "ClueRevealRejectReason",
        "ClueRevealContractError",
        "transition_clue_reveal",
    ):
        assert getattr(module, name, None) is not None


def test_transition_contracts_are_exported_from_session_control() -> None:
    public_module = importlib.import_module("game_runtime.session_control")
    for name in (
        "ClueRevealState",
        "ClueRevealRequest",
        "ClueRevealAccepted",
        "ClueRevealRejected",
        "ClueRevealDecision",
        "ClueRevealEffect",
        "ClueRevealRejectReason",
        "ClueRevealContractError",
        "ClueRevealContractFailureReason",
        "transition_clue_reveal",
    ):
        assert getattr(public_module, name, None) is getattr(_module(), name)


def test_available_clue_produces_one_public_reveal_decision() -> None:
    module = _module()
    request = module.ClueRevealRequest(
        clue_id="clue-1",
        current_state=_active_state(suffix="1"),
        candidate_state=_active_state(suffix="2"),
        disposition=ClueRevealDisposition.AVAILABLE,
    )

    decision = module.transition_clue_reveal(request)

    assert isinstance(decision, module.ClueRevealAccepted)
    assert decision.clue_id == "clue-1"
    assert decision.previous_state == request.current_state
    assert decision.resulting_state == request.candidate_state
    assert decision.effect is module.ClueRevealEffect.PUBLIC_REVEAL
    assert not hasattr(decision, "__dict__")
    with pytest.raises(FrozenInstanceError):
        decision.clue_id = "clue-other"


@pytest.mark.parametrize(
    ("disposition", "reason_name"),
    [
        (ClueRevealDisposition.RULE_SET_NOT_ACTIVE, "RULE_SET_NOT_ACTIVE"),
        (ClueRevealDisposition.ALREADY_REVEALED, "CLUE_ALREADY_REVEALED"),
        (ClueRevealDisposition.NOT_FOUND, "CLUE_NOT_FOUND"),
        (ClueRevealDisposition.NOT_REVEALABLE, "CLUE_NOT_REVEALABLE"),
    ],
)
def test_non_available_disposition_returns_business_rejection(
    disposition: ClueRevealDisposition,
    reason_name: str,
) -> None:
    module = _module()
    current = (
        module.ClueRevealState(None, None, None)
        if disposition is ClueRevealDisposition.RULE_SET_NOT_ACTIVE
        else _active_state()
    )
    request = module.ClueRevealRequest(
        clue_id="clue-1",
        current_state=current,
        candidate_state=None,
        disposition=disposition,
    )

    decision = module.transition_clue_reveal(request)

    assert isinstance(decision, module.ClueRevealRejected)
    assert decision.reason is getattr(module.ClueRevealRejectReason, reason_name)


def test_unknown_disposition_and_malformed_state_fail_closed() -> None:
    module = _module()
    with pytest.raises(module.ClueRevealContractError):
        module.ClueRevealRequest(
            clue_id="clue-1",
            current_state=_active_state(),
            candidate_state=None,
            disposition=ClueRevealDisposition.UNKNOWN,
        )
    with pytest.raises(module.ClueRevealContractError):
        module.ClueRevealState(
            rule_set_reference="rule-set:commit-1",
            disclosure_state_reference=None,
            hidden_state_reference="hidden-state:commit-1",
        )


@pytest.mark.parametrize(
    "candidate",
    [
        None,
        "same",
        "different-rule-set",
        "same-disclosure",
        "same-hidden",
    ],
)
def test_available_request_requires_complete_advancing_candidate(candidate: object) -> None:
    module = _module()
    current = _active_state(suffix="1")
    if candidate == "same":
        candidate = current
    elif candidate == "different-rule-set":
        candidate = replace(_active_state(suffix="2"), rule_set_reference="rule-set:other")
    elif candidate == "same-disclosure":
        candidate = replace(
            _active_state(suffix="2"),
            disclosure_state_reference=current.disclosure_state_reference,
        )
    elif candidate == "same-hidden":
        candidate = replace(
            _active_state(suffix="2"),
            hidden_state_reference=current.hidden_state_reference,
        )

    with pytest.raises(module.ClueRevealContractError):
        module.ClueRevealRequest(
            clue_id="clue-1",
            current_state=current,
            candidate_state=candidate,
            disposition=ClueRevealDisposition.AVAILABLE,
        )


def test_transition_is_deterministic_and_rejects_non_request() -> None:
    module = _module()
    request = module.ClueRevealRequest(
        clue_id="clue-1",
        current_state=_active_state(suffix="1"),
        candidate_state=_active_state(suffix="2"),
        disposition=ClueRevealDisposition.AVAILABLE,
    )

    assert module.transition_clue_reveal(request) == module.transition_clue_reveal(request)
    with pytest.raises(TypeError, match="request"):
        module.transition_clue_reveal(object())
