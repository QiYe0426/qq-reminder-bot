from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

import game_runtime.session_control as session_control
from game_runtime.session_control import (
    CompositeSnapshotContractError,
    CompositeSnapshotFailureReason,
    GameRuleSnapshotSlice,
    HiddenGameStateSlice,
)
from test_composite_game_snapshot_contract import _candidate


def _quest_type():
    value = getattr(session_control, "QuestSnapshotSlice", None)
    assert value is not None, "QuestSnapshotSlice contract is missing"
    return value


def test_quest_slice_is_frozen_slotted_and_exact_set() -> None:
    quest_type = _quest_type()
    empty = quest_type(1, 0, None, None, None)
    active = quest_type(
        1,
        1,
        "quest-1",
        "rule-set:commit-1",
        "quest-public:commit-1",
    )

    assert not hasattr(empty, "__dict__")
    assert active.active_quest_id == "quest-1"
    with pytest.raises(FrozenInstanceError):
        active.active_quest_id = "quest-other"
    with pytest.raises(CompositeSnapshotContractError) as partial:
        quest_type(1, 1, "quest-1", None, "quest-public:commit-1")
    assert partial.value.reason is CompositeSnapshotFailureReason.INCOMPLETE_SLICE
    with pytest.raises(CompositeSnapshotContractError) as empty_version:
        quest_type(1, 1, None, None, None)
    assert (
        empty_version.value.reason
        is CompositeSnapshotFailureReason.INVALID_DOMAIN_VERSION
    )


def test_candidate_cross_binds_quest_to_rule_set_and_hidden_state() -> None:
    quest_type = _quest_type()
    base = _candidate()
    active = replace(
        base,
        game_rules=GameRuleSnapshotSlice(
            2,
            1,
            "rule-set:commit-1",
            "disclosure-state:commit-1",
        ),
        quest=quest_type(
            1,
            1,
            "quest-1",
            "rule-set:commit-1",
            "quest-public:commit-1",
        ),
        hidden_state=HiddenGameStateSlice(
            1,
            1,
            "hidden-state:commit-1",
        ),
    )

    assert active.quest.active_quest_id == "quest-1"
    with pytest.raises(CompositeSnapshotContractError) as scope:
        replace(
            active,
            quest=replace(
                active.quest,
                source_rule_set_reference="rule-set:other",
            ),
        )
    assert (
        scope.value.reason
        is CompositeSnapshotFailureReason.QUEST_RULE_SET_BINDING_MISMATCH
    )


def test_snapshot_identity_includes_quest_domain_version_deterministically() -> None:
    identity = session_control.GameSnapshotIdentity.from_snapshot(_candidate())

    assert identity.domain_versions == (2, 1, 0, 1, 0, 0, 0)
    assert identity == session_control.GameSnapshotIdentity.from_snapshot(
        _candidate()
    )
