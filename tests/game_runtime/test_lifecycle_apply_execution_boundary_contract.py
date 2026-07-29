from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, fields, replace
import inspect

import pytest

from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control import (
    ActorOwnedLifecycleSnapshotCommitBoundary,
    BuildPlanReady,
    CandidateSessionSnapshot,
    ControlTurnCommitReady,
    LifecycleSnapshotIdentity,
    SnapshotVisibilityAccepted,
    SnapshotVisibilityBoundaryError,
    SnapshotVisibilityFailureReason,
)
from test_actor_control_turn_integration_contract import (
    make_build_outcome,
    make_commit_ready,
    make_receipt,
)


def make_current_snapshot(
    *,
    group_id: str = "group-1",
    state_version: int = 4,
    cursor: int = 6,
) -> CandidateSessionSnapshot:
    return CandidateSessionSnapshot(
        game_id="game-1",
        session_id="session-1",
        group_id=group_id,
        dm_participant_id="participant-dm",
        status=GameSessionStatus.RUNNING,
        current_phase=GamePhase.EXPLORATION,
        state_version=state_version,
        last_applied_sequence_no=cursor,
    )


def make_boundary(
    *,
    snapshot: CandidateSessionSnapshot | None = None,
    ownership_generation: int | None = 3,
) -> ActorOwnedLifecycleSnapshotCommitBoundary:
    return ActorOwnedLifecycleSnapshotCommitBoundary(
        current_snapshot=snapshot or make_current_snapshot(),
        ownership_generation=ownership_generation,
    )


def assert_boundary_failure(
    boundary: ActorOwnedLifecycleSnapshotCommitBoundary,
    value: object,
    reason: SnapshotVisibilityFailureReason,
) -> None:
    before = boundary.current_snapshot
    with pytest.raises(SnapshotVisibilityBoundaryError) as captured:
        boundary.accept(value)  # type: ignore[arg-type]
    assert captured.value.reason is reason
    assert boundary.current_snapshot is before


def test_snapshot_visibility_contracts_are_immutable_value_only() -> None:
    snapshot = make_current_snapshot()
    identity = LifecycleSnapshotIdentity.from_snapshot(snapshot)
    accepted = SnapshotVisibilityAccepted(
        game_id="game-1",
        session_id="session-1",
        operation_id="operation-7",
        command_id="command-7",
        input_event_id="event-7",
        input_sequence_no=7,
        previous_state_version=4,
        committed_state_version=5,
        previous_cursor=6,
        committed_cursor=7,
        previous_snapshot_identity=identity,
        committed_snapshot_identity=replace(
            identity,
            lifecycle_status=GameSessionStatus.PAUSED,
            state_version=5,
            last_applied_sequence_no=7,
        ),
        ownership_generation=3,
        commit_evidence_reference="commit-evidence-1",
        already_visible=False,
    )
    forbidden = ("actor", "port", "callback", "task", "repository")

    for value in (identity, accepted):
        assert not hasattr(value, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(value, fields(value)[0].name, None)
        for field in fields(value):
            contract = f"{field.name} {field.type}".lower()
            assert not any(term in contract for term in forbidden)


def test_accept_is_synchronous_and_raw_receipt_is_rejected() -> None:
    boundary = make_boundary()
    outcome = make_build_outcome()

    assert not inspect.iscoroutinefunction(boundary.accept)
    assert_boundary_failure(
        boundary,
        make_receipt(outcome),
        SnapshotVisibilityFailureReason.INVALID_COMMIT_READY,
    )


def test_build_reject_is_not_a_snapshot_replacement() -> None:
    boundary = make_boundary()

    assert_boundary_failure(
        boundary,
        make_commit_ready(rejected=True),
        SnapshotVisibilityFailureReason.BUILD_REJECT_NOT_REPLACEABLE,
    )


def test_invalid_receipt_proof_is_rejected_again_at_boundary() -> None:
    ready = make_commit_ready()
    forged = object.__new__(ControlTurnCommitReady)
    object.__setattr__(forged, "build_outcome", ready.build_outcome)
    object.__setattr__(
        forged,
        "accepted_receipt",
        replace(ready.accepted_receipt, committed_state_version=6),
    )

    assert_boundary_failure(
        make_boundary(),
        forged,
        SnapshotVisibilityFailureReason.RECEIPT_BINDING_MISMATCH,
    )


def test_operation_identity_mismatch_is_typed_separately() -> None:
    ready = make_commit_ready()
    forged = object.__new__(ControlTurnCommitReady)
    object.__setattr__(forged, "build_outcome", ready.build_outcome)
    object.__setattr__(
        forged,
        "accepted_receipt",
        replace(ready.accepted_receipt, operation_id="other-operation"),
    )

    assert_boundary_failure(
        make_boundary(),
        forged,
        SnapshotVisibilityFailureReason.OPERATION_IDENTITY_MISMATCH,
    )


def test_success_replaces_snapshot_once_and_returns_visibility_proof() -> None:
    boundary = make_boundary()
    before = boundary.current_snapshot
    ready = make_commit_ready()
    assert isinstance(ready.build_outcome, BuildPlanReady)
    candidate = ready.build_outcome.plan.candidate_snapshot

    accepted = boundary.accept(ready)

    assert isinstance(accepted, SnapshotVisibilityAccepted)
    assert accepted.already_visible is False
    assert boundary.current_snapshot is candidate
    assert boundary.current_snapshot is not before
    assert accepted.previous_state_version == 4
    assert accepted.committed_state_version == 5
    assert accepted.previous_cursor == 6
    assert accepted.committed_cursor == 7
    assert boundary.ownership_generation == 3
    with pytest.raises(FrozenInstanceError):
        candidate.state_version = 6  # type: ignore[misc]


def test_exact_duplicate_is_idempotent_without_second_replacement() -> None:
    boundary = make_boundary()
    ready = make_commit_ready()
    first = boundary.accept(ready)
    installed = boundary.current_snapshot

    second = boundary.accept(ready)

    assert first.already_visible is False
    assert second.already_visible is True
    assert boundary.current_snapshot is installed
    assert second.committed_snapshot_identity == first.committed_snapshot_identity


def test_same_operation_with_different_snapshot_fault_stops() -> None:
    boundary = make_boundary()
    ready = make_commit_ready()
    boundary.accept(ready)
    assert isinstance(ready.build_outcome, BuildPlanReady)
    conflicting_plan = replace(
        ready.build_outcome.plan,
        candidate_snapshot=replace(
            ready.build_outcome.plan.candidate_snapshot,
            current_phase=GamePhase.DISCUSSION,
        ),
    )
    conflicting = ControlTurnCommitReady(
        build_outcome=BuildPlanReady(plan=conflicting_plan),
        accepted_receipt=ready.accepted_receipt,
    )

    assert_boundary_failure(
        boundary,
        conflicting,
        SnapshotVisibilityFailureReason.ALREADY_VISIBLE_CONFLICT,
    )


@pytest.mark.parametrize(
    ("boundary", "reason"),
    [
        (
            make_boundary(snapshot=make_current_snapshot(state_version=3)),
            SnapshotVisibilityFailureReason.VERSION_MISMATCH,
        ),
        (
            make_boundary(snapshot=make_current_snapshot(cursor=5)),
            SnapshotVisibilityFailureReason.CURSOR_MISMATCH,
        ),
        (
            make_boundary(ownership_generation=2),
            SnapshotVisibilityFailureReason.OWNERSHIP_MISMATCH,
        ),
        (
            make_boundary(snapshot=make_current_snapshot(group_id="other-group")),
            SnapshotVisibilityFailureReason.SNAPSHOT_IDENTITY_MISMATCH,
        ),
    ],
)
def test_invariant_mismatch_never_replaces_snapshot(
    boundary: ActorOwnedLifecycleSnapshotCommitBoundary,
    reason: SnapshotVisibilityFailureReason,
) -> None:
    assert_boundary_failure(boundary, make_commit_ready(), reason)


def test_boundary_has_no_forbidden_runtime_or_apply_dependencies() -> None:
    module = inspect.getmodule(ActorOwnedLifecycleSnapshotCommitBoundary)
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

    assert not any(
        forbidden in imported
        for imported in imports
        for forbidden in (
            "persistence",
            "recovery",
            "notification",
            "event_store",
            "lifecycle_phase_builder",
        )
    )
    assert calls.isdisjoint(
        {
            "transition_to",
            "transition_phase_to",
            "commit_control_apply",
            "append_event",
            "notify",
            "recover",
            "retry",
        }
    )
