from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, replace
import inspect

import pytest

import game_runtime.session_control as session_control
import game_runtime.session_control.actor_visible_game_state as actor_state_module
import game_runtime.session_control.composite_snapshot as composite_module
from game_runtime.participant import (
    ParticipantMembershipState,
    ParticipantType,
)
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control.apply_contract import CandidateSessionSnapshot
from game_runtime.session_control.actor_visible_game_state import (
    ActorVisibleGameState,
    ControlCompletionIdentity,
    ControlCompletionKind,
)
from game_runtime.session_control.composite_snapshot import (
    CandidateGameSnapshot,
    CompositeSnapshotContractError,
    CompositeSnapshotFailureReason,
    GameRuleSnapshotSlice,
    GameSnapshotIdentity,
    HiddenGameStateSlice,
    LifecycleSnapshotSlice,
    ParticipantSnapshotRecord,
    ParticipantSnapshotSlice,
    PhaseSnapshotSlice,
    SetupSnapshotSlice,
)
from test_control_apply_port import apply_plan as legacy_apply_plan


def _candidate(
    *,
    game_id: str = "game-1",
    session_id: str = "session-1",
    group_id: str = "group-1",
    dm_participant_id: str = "dm-1",
    status: GameSessionStatus = GameSessionStatus.RUNNING,
    phase: GamePhase = GamePhase.INTRODUCTION,
    state_version: int = 4,
    materialization_cursor: int = 7,
) -> CandidateGameSnapshot:
    return CandidateGameSnapshot(
        game_id=game_id,
        session_id=session_id,
        group_id=group_id,
        dm_participant_id=dm_participant_id,
        status=status,
        current_phase=phase,
        state_version=state_version,
        last_applied_sequence_no=materialization_cursor,
        snapshot_schema_version=2,
        lifecycle=LifecycleSnapshotSlice(
            schema_version=1,
            domain_version=2,
            status=status,
        ),
        phase=PhaseSnapshotSlice(
            schema_version=1,
            domain_version=1,
            phase=phase,
        ),
        setup=SetupSnapshotSlice(
            schema_version=1,
            domain_version=0,
            script_id=None,
            public_name=None,
            manifest_reference=None,
            manifest_version=None,
        ),
        participants=ParticipantSnapshotSlice(
            schema_version=1,
            domain_version=1,
            participants=(
                ParticipantSnapshotRecord(
                    participant_id=dm_participant_id,
                    participant_type=ParticipantType.DM,
                    membership_state=ParticipantMembershipState.ACTIVE,
                    character_id=None,
                    binding_version=0,
                ),
                ParticipantSnapshotRecord(
                    participant_id="player-1",
                    participant_type=ParticipantType.PLAYER,
                    membership_state=ParticipantMembershipState.ACTIVE,
                    character_id="character-1",
                    binding_version=1,
                ),
            ),
        ),
        game_rules=GameRuleSnapshotSlice(
            schema_version=2,
            domain_version=0,
            committed_rule_set_reference=None,
            committed_disclosure_state_reference=None,
        ),
        hidden_state=HiddenGameStateSlice(
            schema_version=1,
            domain_version=0,
            committed_state_reference=None,
        ),
    )


def test_composite_snapshot_contracts_are_frozen_and_nominally_compatible() -> None:
    candidate = _candidate()

    assert isinstance(candidate, CandidateSessionSnapshot)
    assert not hasattr(candidate, "__dict__")
    assert not hasattr(candidate.lifecycle, "__dict__")
    assert not hasattr(candidate.participants.participants[0], "__dict__")
    with pytest.raises(FrozenInstanceError):
        candidate.state_version = 5  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        candidate.lifecycle.domain_version = 3  # type: ignore[misc]


def test_game_snapshot_identity_is_deterministic_and_value_only() -> None:
    candidate = _candidate()

    first = GameSnapshotIdentity.from_snapshot(candidate)
    second = GameSnapshotIdentity.from_snapshot(candidate)

    assert first == second
    assert first.game_id == candidate.game_id
    assert first.session_id == candidate.session_id
    assert first.state_version == candidate.state_version
    assert (
        first.snapshot_materialization_cursor
        == candidate.last_applied_sequence_no
    )
    assert first.domain_versions == (2, 1, 0, 1, 0, 0)
    assert not hasattr(first, "__dict__")

    with pytest.raises(TypeError):
        replace(first, domain_versions=list(first.domain_versions))  # type: ignore[arg-type]
    with pytest.raises(CompositeSnapshotContractError) as schema_error:
        replace(first, snapshot_schema_version=1)
    assert (
        schema_error.value.reason
        is CompositeSnapshotFailureReason.UNSUPPORTED_SCHEMA_VERSION
    )
    with pytest.raises(CompositeSnapshotContractError) as version_error:
        replace(first, domain_versions=(2, 1, 0, -1, 0, 0))
    assert (
        version_error.value.reason
        is CompositeSnapshotFailureReason.INVALID_DOMAIN_VERSION
    )


def test_snapshot_rejects_unsupported_schema_or_domain_versions() -> None:
    with pytest.raises(CompositeSnapshotContractError) as exc_info:
        replace(_candidate(), snapshot_schema_version=1)
    assert (
        exc_info.value.reason
        is CompositeSnapshotFailureReason.UNSUPPORTED_SCHEMA_VERSION
    )

    with pytest.raises(CompositeSnapshotContractError) as lifecycle_error:
        LifecycleSnapshotSlice(
            schema_version=2,
            domain_version=2,
            status=GameSessionStatus.RUNNING,
        )
    assert (
        lifecycle_error.value.reason
        is CompositeSnapshotFailureReason.UNSUPPORTED_SCHEMA_VERSION
    )

    with pytest.raises(CompositeSnapshotContractError) as phase_error:
        PhaseSnapshotSlice(
            schema_version=1,
            domain_version=-1,
            phase=GamePhase.INTRODUCTION,
        )
    assert (
        phase_error.value.reason
        is CompositeSnapshotFailureReason.INVALID_DOMAIN_VERSION
    )


def test_setup_slice_requires_an_exact_empty_or_complete_field_set() -> None:
    empty = _candidate().setup
    assert empty.domain_version == 0

    complete = SetupSnapshotSlice(
        schema_version=1,
        domain_version=1,
        script_id="script-1",
        public_name="Public Script",
        manifest_reference="manifest-1",
        manifest_version=3,
    )
    assert complete.script_id == "script-1"

    with pytest.raises(CompositeSnapshotContractError) as exc_info:
        SetupSnapshotSlice(
            schema_version=1,
            domain_version=1,
            script_id="script-1",
            public_name=None,
            manifest_reference="manifest-1",
            manifest_version=3,
        )
    assert exc_info.value.reason is CompositeSnapshotFailureReason.INCOMPLETE_SLICE


def test_participant_slice_requires_tuple_canonical_order_and_unique_ids() -> None:
    records = _candidate().participants.participants

    with pytest.raises(TypeError):
        ParticipantSnapshotSlice(
            schema_version=1,
            domain_version=1,
            participants=list(records),  # type: ignore[arg-type]
        )
    with pytest.raises(CompositeSnapshotContractError) as order_error:
        ParticipantSnapshotSlice(
            schema_version=1,
            domain_version=1,
            participants=tuple(reversed(records)),
        )
    assert (
        order_error.value.reason
        is CompositeSnapshotFailureReason.NON_CANONICAL_PARTICIPANTS
    )
    with pytest.raises(CompositeSnapshotContractError) as duplicate_error:
        ParticipantSnapshotSlice(
            schema_version=1,
            domain_version=1,
            participants=(records[0], records[0]),
        )
    assert (
        duplicate_error.value.reason
        is CompositeSnapshotFailureReason.DUPLICATE_PARTICIPANT
    )


def test_candidate_binds_legacy_projection_and_exactly_one_dm_record() -> None:
    candidate = _candidate()

    with pytest.raises(CompositeSnapshotContractError) as lifecycle_error:
        replace(
            candidate,
            lifecycle=replace(
                candidate.lifecycle,
                status=GameSessionStatus.PAUSED,
            ),
        )
    assert (
        lifecycle_error.value.reason
        is CompositeSnapshotFailureReason.LEGACY_PROJECTION_MISMATCH
    )

    without_dm = replace(
        candidate.participants,
        participants=(candidate.participants.participants[1],),
    )
    with pytest.raises(CompositeSnapshotContractError) as dm_error:
        replace(candidate, participants=without_dm)
    assert (
        dm_error.value.reason
        is CompositeSnapshotFailureReason.DM_BINDING_MISMATCH
    )


def test_hidden_state_accepts_only_an_opaque_committed_reference() -> None:
    hidden = HiddenGameStateSlice(
        schema_version=1,
        domain_version=1,
        committed_state_reference="hidden-state:commit-1",
    )
    assert hidden.committed_state_reference == "hidden-state:commit-1"

    with pytest.raises(TypeError):
        HiddenGameStateSlice(
            schema_version=1,
            domain_version=1,
            committed_state_reference={"secret": "value"},  # type: ignore[arg-type]
        )


def test_candidate_requires_game_rule_and_hidden_state_references_together() -> None:
    candidate = _candidate()
    active_rules = GameRuleSnapshotSlice(
        schema_version=2,
        domain_version=3,
        committed_rule_set_reference="rule-set:commit-1",
        committed_disclosure_state_reference="disclosure-state:commit-1",
    )
    active_hidden = HiddenGameStateSlice(
        schema_version=1,
        domain_version=5,
        committed_state_reference="hidden-state:commit-1",
    )

    active = replace(candidate, game_rules=active_rules, hidden_state=active_hidden)
    assert active.game_rules.domain_version == 3
    assert active.hidden_state.domain_version == 5

    for mismatch in (
        {"game_rules": active_rules},
        {"hidden_state": active_hidden},
    ):
        with pytest.raises(CompositeSnapshotContractError) as error:
            replace(candidate, **mismatch)
        assert (
            error.value.reason
            is CompositeSnapshotFailureReason.GAME_RULE_HIDDEN_BINDING_MISMATCH
        )


def test_game_rule_snapshot_v2_requires_rule_and_disclosure_references_together() -> None:
    game_rule_schema = getattr(
        composite_module, "GAME_RULE_SLICE_SCHEMA_VERSION", None
    )
    disclosure_reason = getattr(
        CompositeSnapshotFailureReason,
        "GAME_RULE_DISCLOSURE_BINDING_MISMATCH",
        None,
    )

    assert composite_module.COMPOSITE_SNAPSHOT_SCHEMA_VERSION == 2
    assert game_rule_schema == 2
    assert disclosure_reason is not None
    empty = GameRuleSnapshotSlice(
        schema_version=game_rule_schema,
        domain_version=0,
        committed_rule_set_reference=None,
        committed_disclosure_state_reference=None,
    )
    active = GameRuleSnapshotSlice(
        schema_version=game_rule_schema,
        domain_version=1,
        committed_rule_set_reference="rule-set:commit-1",
        committed_disclosure_state_reference="disclosure-state:commit-1",
    )

    assert empty.domain_version == 0
    assert active.committed_disclosure_state_reference == "disclosure-state:commit-1"
    for values in (
        {
            "committed_rule_set_reference": "rule-set:commit-1",
            "committed_disclosure_state_reference": None,
        },
        {
            "committed_rule_set_reference": None,
            "committed_disclosure_state_reference": "disclosure-state:commit-1",
        },
    ):
        with pytest.raises(CompositeSnapshotContractError) as error:
            GameRuleSnapshotSlice(
                schema_version=game_rule_schema,
                domain_version=1,
                **values,
            )
        assert error.value.reason is disclosure_reason


def _completion(
    *,
    kind: ControlCompletionKind,
    sequence: int,
    state_version: int = 4,
    game_id: str = "game-1",
    session_id: str = "session-1",
) -> ControlCompletionIdentity:
    return ControlCompletionIdentity(
        game_id=game_id,
        session_id=session_id,
        command_id="command-1",
        operation_id=f"operation-{sequence}",
        input_event_id=f"event-{sequence}",
        input_sequence_no=sequence,
        state_version=state_version,
        commit_evidence_reference=f"commit-{sequence}",
        completion_kind=kind,
    )


def test_actor_visible_state_is_immutable_and_accepts_initial_or_applied_state() -> None:
    candidate = _candidate()
    initial = ActorVisibleGameState(
        snapshot=candidate,
        committed_control_cursor=7,
        ownership_generation=3,
        last_completion_identity=None,
    )
    applied = ActorVisibleGameState(
        snapshot=candidate,
        committed_control_cursor=7,
        ownership_generation=3,
        last_completion_identity=_completion(
            kind=ControlCompletionKind.APPLIED,
            sequence=7,
        ),
    )

    assert initial.snapshot is candidate
    assert applied.committed_control_cursor == 7
    assert not hasattr(applied, "__dict__")
    with pytest.raises(FrozenInstanceError):
        applied.committed_control_cursor = 8  # type: ignore[misc]


def test_actor_visible_state_separates_reject_cursor_from_snapshot_cursor() -> None:
    candidate = _candidate(materialization_cursor=7)

    state = ActorVisibleGameState(
        snapshot=candidate,
        committed_control_cursor=9,
        ownership_generation=3,
        last_completion_identity=_completion(
            kind=ControlCompletionKind.REJECTED,
            sequence=9,
        ),
    )

    assert state.snapshot.last_applied_sequence_no == 7
    assert state.committed_control_cursor == 9


@pytest.mark.parametrize(
    ("cursor", "completion"),
    [
        (6, None),
        (
            8,
            _completion(
                kind=ControlCompletionKind.APPLIED,
                sequence=8,
            ),
        ),
        (
            7,
            _completion(
                kind=ControlCompletionKind.REJECTED,
                sequence=7,
            ),
        ),
        (
            8,
            _completion(
                kind=ControlCompletionKind.REJECTED,
                sequence=7,
            ),
        ),
    ],
)
def test_actor_visible_state_rejects_invalid_cursor_or_completion_binding(
    cursor: int,
    completion: ControlCompletionIdentity | None,
) -> None:
    with pytest.raises(CompositeSnapshotContractError) as exc_info:
        ActorVisibleGameState(
            snapshot=_candidate(),
            committed_control_cursor=cursor,
            ownership_generation=3,
            last_completion_identity=completion,
        )

    assert exc_info.value.reason in {
        CompositeSnapshotFailureReason.INVALID_CONTROL_CURSOR,
        CompositeSnapshotFailureReason.COMPLETION_IDENTITY_MISMATCH,
    }


@pytest.mark.parametrize(
    "completion",
    [
        _completion(
            kind=ControlCompletionKind.REJECTED,
            sequence=8,
            game_id="other-game",
        ),
        _completion(
            kind=ControlCompletionKind.REJECTED,
            sequence=8,
            session_id="other-session",
        ),
        _completion(
            kind=ControlCompletionKind.REJECTED,
            sequence=8,
            state_version=5,
        ),
    ],
)
def test_actor_visible_state_rejects_scope_or_version_mismatch(
    completion: ControlCompletionIdentity,
) -> None:
    with pytest.raises(CompositeSnapshotContractError) as exc_info:
        ActorVisibleGameState(
            snapshot=_candidate(),
            committed_control_cursor=8,
            ownership_generation=3,
            last_completion_identity=completion,
        )

    assert exc_info.value.reason in {
        CompositeSnapshotFailureReason.SCOPE_MISMATCH,
        CompositeSnapshotFailureReason.COMPLETION_IDENTITY_MISMATCH,
    }


def test_actor_visible_state_rejects_non_positive_ownership_generation() -> None:
    with pytest.raises(CompositeSnapshotContractError) as exc_info:
        ActorVisibleGameState(
            snapshot=_candidate(),
            committed_control_cursor=7,
            ownership_generation=0,
            last_completion_identity=None,
        )

    assert (
        exc_info.value.reason
        is CompositeSnapshotFailureReason.INVALID_SLICE_VALUE
    )


def test_public_exports_and_existing_apply_plan_accept_composite_candidate() -> None:
    expected_exports = {
        "ActorVisibleGameState",
        "CandidateGameSnapshot",
        "CompositeSnapshotContractError",
        "CompositeSnapshotFailureReason",
        "ControlCompletionIdentity",
        "ControlCompletionKind",
        "GameRuleSnapshotSlice",
        "GameSnapshotIdentity",
        "HiddenGameStateSlice",
        "LifecycleSnapshotSlice",
        "ParticipantSnapshotRecord",
        "ParticipantSnapshotSlice",
        "PhaseSnapshotSlice",
        "SetupSnapshotSlice",
    }
    assert expected_exports.issubset(set(session_control.__all__))
    assert all(hasattr(session_control, name) for name in expected_exports)

    legacy = legacy_apply_plan()
    candidate = _candidate(
        game_id=legacy.game_id,
        session_id=legacy.session_id,
        group_id=legacy.group_id,
        dm_participant_id=legacy.candidate_snapshot.dm_participant_id,
        status=legacy.candidate_snapshot.status,
        phase=legacy.candidate_snapshot.current_phase,
        state_version=legacy.candidate_snapshot.state_version,
        materialization_cursor=legacy.candidate_snapshot.last_applied_sequence_no,
    )

    composite_plan = replace(legacy, candidate_snapshot=candidate)

    assert composite_plan.candidate_snapshot is candidate
    assert isinstance(composite_plan.candidate_snapshot, CandidateSessionSnapshot)


def test_candidate_rejects_a_missing_domain_slice() -> None:
    with pytest.raises(TypeError):
        replace(_candidate(), setup=None)  # type: ignore[arg-type]


def test_contract_modules_have_no_runtime_or_io_dependencies() -> None:
    forbidden_roots = {
        "asyncio",
        "game_runtime.actor",
        "game_runtime.persistence",
        "game_runtime.recovery",
        "openai",
        "plugins",
        "requests",
    }
    forbidden_calls = {
        "append",
        "commit",
        "coordinate",
        "open",
        "send",
    }

    for module in (composite_module, actor_state_module):
        tree = ast.parse(inspect.getsource(module))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        called_attributes = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        }

        assert all(
            not any(
                imported_name == root or imported_name.startswith(f"{root}.")
                for root in forbidden_roots
            )
            for imported_name in imported
        )
        assert called_attributes.isdisjoint(forbidden_calls)
