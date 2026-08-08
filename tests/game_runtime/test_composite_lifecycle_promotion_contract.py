from __future__ import annotations

import ast
import dataclasses
import inspect
from dataclasses import fields, replace

import pytest

import game_runtime.session_control.composite_lifecycle_builder as builder_module
from game_runtime.participant import (
    ParticipantMembershipState,
    ParticipantType,
)
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control import (
    BuildNonCommit,
    BuildNonCommitReason,
    BuildPlanReady,
    BuildReject,
    CandidateGameSnapshot,
    CompositeLifecycleControlApplyPlanBuilder,
    ControlApplyBuildContextError,
    GameRuleSnapshotSlice,
    GameSnapshotIdentity,
    HiddenGameStateSlice,
    LifecycleSnapshotSlice,
    ParticipantSnapshotRecord,
    ParticipantSnapshotSlice,
    PhaseSnapshotSlice,
    QuestSnapshotSlice,
    SessionCommandType,
    SetupSnapshotSlice,
)
from game_runtime.session_control.apply_contract import ControlApplyPlan
from game_runtime.session_control.lifecycle_phase_builder import (
    LifecycleControlApplyPlanBuilder,
)
from test_lifecycle_phase_apply_plan_builder import make_context


def _current_snapshot(
    context,
    *,
    materialization_cursor: int | None = None,
) -> CandidateGameSnapshot:
    session = context.session_view
    setup = context.setup_view
    assert setup is not None
    if materialization_cursor is None:
        materialization_cursor = session.last_applied_sequence_no
    participant_records = tuple(
        ParticipantSnapshotRecord(
            participant_id=view.participant_id,
            participant_type=view.participant_type,
            membership_state=view.membership_state,
            character_id=view.character_id,
            binding_version=view.binding_version,
        )
        for view in sorted(
            context.participant_views,
            key=lambda item: item.participant_id,
        )
    )
    return CandidateGameSnapshot(
        game_id=session.game_id,
        session_id=session.session_id,
        group_id=session.group_id,
        dm_participant_id=session.dm_participant_id,
        status=session.status,
        current_phase=session.current_phase,
        state_version=session.state_version,
        last_applied_sequence_no=materialization_cursor,
        snapshot_schema_version=3,
        lifecycle=LifecycleSnapshotSlice(
            schema_version=1,
            domain_version=2,
            status=session.status,
        ),
        phase=PhaseSnapshotSlice(
            schema_version=1,
            domain_version=3,
            phase=session.current_phase,
        ),
        setup=SetupSnapshotSlice(
            schema_version=1,
            domain_version=setup.setup_version,
            script_id=setup.script_id,
            public_name=setup.public_name,
            manifest_reference=setup.manifest_reference,
            manifest_version=1,
        ),
        participants=ParticipantSnapshotSlice(
            schema_version=1,
            domain_version=1,
            participants=participant_records,
        ),
        game_rules=GameRuleSnapshotSlice(
            schema_version=2,
            domain_version=1,
            committed_rule_set_reference="rule-set:commit-1",
            committed_disclosure_state_reference="disclosure-state:commit-1",
        ),
        quest=QuestSnapshotSlice(1, 0, None, None, None),
        hidden_state=HiddenGameStateSlice(
            schema_version=1,
            domain_version=1,
            committed_state_reference="hidden-state:commit-1",
        ),
    )


def _context(
    command_type: SessionCommandType,
    *,
    status: GameSessionStatus,
    phase: GamePhase,
    materialization_cursor: int | None = None,
):
    context = make_context(
        command_type,
        status=status,
        current_phase=phase,
    )
    snapshot = _current_snapshot(
        context,
        materialization_cursor=materialization_cursor,
    )
    return replace(
        context,
        session_view=replace(
            context.session_view,
            current_game_snapshot=snapshot,
        ),
    )


def test_session_view_keeps_legacy_context_compatible_without_composite() -> None:
    context = make_context(
        SessionCommandType.START_GAME,
        status=GameSessionStatus.CREATED,
        current_phase=GamePhase.LOBBY,
    )

    assert context.session_view.current_game_snapshot is None


def test_session_view_accepts_materialization_cursor_behind_control_cursor() -> None:
    context = _context(
        SessionCommandType.PAUSE_GAME,
        status=GameSessionStatus.RUNNING,
        phase=GamePhase.EXPLORATION,
        materialization_cursor=4,
    )

    assert context.session_view.last_applied_sequence_no == 6
    assert (
        context.session_view.current_game_snapshot.last_applied_sequence_no
        == 4
    )


@pytest.mark.parametrize(
    "snapshot_change",
    [
        {"game_id": "other-game"},
        {"state_version": 3},
        {"last_applied_sequence_no": 7},
    ],
)
def test_session_view_rejects_mismatched_composite_binding(
    snapshot_change: dict[str, object],
) -> None:
    context = make_context(
        SessionCommandType.PAUSE_GAME,
        status=GameSessionStatus.RUNNING,
        current_phase=GamePhase.EXPLORATION,
    )
    snapshot = replace(_current_snapshot(context), **snapshot_change)

    with pytest.raises(ControlApplyBuildContextError):
        replace(
            context.session_view,
            current_game_snapshot=snapshot,
        )


def test_session_view_rejects_mismatched_lifecycle_projection() -> None:
    context = make_context(
        SessionCommandType.PAUSE_GAME,
        status=GameSessionStatus.RUNNING,
        current_phase=GamePhase.EXPLORATION,
    )
    other_context = make_context(
        SessionCommandType.END_GAME,
        status=GameSessionStatus.PAUSED,
        current_phase=GamePhase.EXPLORATION,
    )

    with pytest.raises(ControlApplyBuildContextError):
        replace(
            context.session_view,
            current_game_snapshot=_current_snapshot(other_context),
        )


@pytest.mark.parametrize(
    (
        "command_type",
        "status",
        "phase",
        "expected_status",
        "expected_phase",
        "lifecycle_advance",
        "phase_advance",
    ),
    [
        (
            SessionCommandType.START_GAME,
            GameSessionStatus.CREATED,
            GamePhase.LOBBY,
            GameSessionStatus.RUNNING,
            GamePhase.INTRODUCTION,
            1,
            1,
        ),
        (
            SessionCommandType.PAUSE_GAME,
            GameSessionStatus.RUNNING,
            GamePhase.EXPLORATION,
            GameSessionStatus.PAUSED,
            GamePhase.EXPLORATION,
            1,
            0,
        ),
        (
            SessionCommandType.END_GAME,
            GameSessionStatus.CREATED,
            GamePhase.LOBBY,
            GameSessionStatus.ENDED,
            GamePhase.ENDING,
            1,
            1,
        ),
        (
            SessionCommandType.END_GAME,
            GameSessionStatus.RUNNING,
            GamePhase.DISCUSSION,
            GameSessionStatus.ENDED,
            GamePhase.ENDING,
            1,
            1,
        ),
        (
            SessionCommandType.END_GAME,
            GameSessionStatus.PAUSED,
            GamePhase.EXPLORATION,
            GameSessionStatus.ENDED,
            GamePhase.ENDING,
            1,
            1,
        ),
    ],
)
def test_lifecycle_success_is_promoted_to_complete_composite_candidate(
    command_type: SessionCommandType,
    status: GameSessionStatus,
    phase: GamePhase,
    expected_status: GameSessionStatus,
    expected_phase: GamePhase,
    lifecycle_advance: int,
    phase_advance: int,
) -> None:
    context = _context(
        command_type,
        status=status,
        phase=phase,
    )
    current = context.session_view.current_game_snapshot
    assert current is not None

    outcome = CompositeLifecycleControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildPlanReady)
    candidate = outcome.plan.candidate_snapshot
    assert isinstance(candidate, CandidateGameSnapshot)
    assert candidate.status is expected_status
    assert candidate.current_phase is expected_phase
    assert (
        candidate.lifecycle.domain_version
        == current.lifecycle.domain_version + lifecycle_advance
    )
    assert (
        candidate.phase.domain_version
        == current.phase.domain_version + phase_advance
    )
    if phase_advance:
        assert candidate.phase is not current.phase
    else:
        assert candidate.phase is current.phase
    assert candidate.setup is current.setup
    assert candidate.participants is current.participants
    assert candidate.game_rules is current.game_rules
    assert candidate.hidden_state is current.hidden_state
    assert candidate.state_version == current.state_version + 1
    assert (
        candidate.last_applied_sequence_no
        == context.envelope.event_sequence_no
    )


def test_promotion_preserves_every_plan_field_except_candidate_snapshot() -> None:
    context = _context(
        SessionCommandType.START_GAME,
        status=GameSessionStatus.CREATED,
        phase=GamePhase.LOBBY,
    )
    legacy = LifecycleControlApplyPlanBuilder().build(context)
    promoted = CompositeLifecycleControlApplyPlanBuilder().build(context)
    assert isinstance(legacy, BuildPlanReady)
    assert isinstance(promoted, BuildPlanReady)

    preserved_names = {
        field.name
        for field in fields(ControlApplyPlan)
        if field.name != "candidate_snapshot"
    }
    assert all(
        getattr(promoted.plan, name) == getattr(legacy.plan, name)
        for name in preserved_names
    )
    assert isinstance(promoted.plan.candidate_snapshot, CandidateGameSnapshot)
    assert not isinstance(legacy.plan.candidate_snapshot, CandidateGameSnapshot)


def test_reject_and_noncommit_bypass_composite_promotion() -> None:
    reject_context = make_context(
        SessionCommandType.END_GAME,
        status=GameSessionStatus.ENDED,
        current_phase=GamePhase.ENDING,
    )
    noncommit_context = make_context(
        SessionCommandType.CHANGE_PHASE,
        status=GameSessionStatus.RUNNING,
        current_phase=GamePhase.EXPLORATION,
    )

    rejected = CompositeLifecycleControlApplyPlanBuilder().build(reject_context)
    noncommit = CompositeLifecycleControlApplyPlanBuilder().build(
        noncommit_context
    )

    assert isinstance(rejected, BuildReject)
    assert isinstance(noncommit, BuildNonCommit)
    assert (
        noncommit.detail_code
        == "CHANGE_PHASE_REDUCER_UNAVAILABLE"
    )


def test_missing_current_snapshot_fails_closed_before_promotion() -> None:
    context = make_context(
        SessionCommandType.START_GAME,
        status=GameSessionStatus.CREATED,
        current_phase=GamePhase.LOBBY,
    )

    outcome = CompositeLifecycleControlApplyPlanBuilder().build(context)

    assert outcome == BuildNonCommit(
        reason=BuildNonCommitReason.EVIDENCE_MISMATCH,
        detail_code="COMPOSITE_CURRENT_SNAPSHOT_MISSING",
    )


@pytest.mark.parametrize(
    ("mutation", "detail_code"),
    [
        (
            "participant",
            "COMPOSITE_PARTICIPANT_BINDING_MISMATCH",
        ),
        (
            "setup",
            "COMPOSITE_SETUP_BINDING_MISMATCH",
        ),
    ],
)
def test_snapshot_view_mismatch_fails_closed(
    mutation: str,
    detail_code: str,
) -> None:
    context = _context(
        SessionCommandType.PAUSE_GAME,
        status=GameSessionStatus.RUNNING,
        phase=GamePhase.EXPLORATION,
    )
    current = context.session_view.current_game_snapshot
    assert current is not None
    if mutation == "participant":
        record = current.participants.participants[0]
        changed = replace(
            current,
            participants=replace(
                current.participants,
                participants=(
                    replace(record, binding_version=record.binding_version + 1),
                ),
            ),
        )
    else:
        changed = replace(
            current,
            setup=replace(current.setup, public_name="Other Script"),
        )
    mismatched = replace(
        context,
        session_view=replace(
            context.session_view,
            current_game_snapshot=changed,
        ),
    )

    outcome = CompositeLifecycleControlApplyPlanBuilder().build(mismatched)

    assert isinstance(outcome, BuildNonCommit)
    assert outcome.reason is BuildNonCommitReason.EVIDENCE_MISMATCH
    assert outcome.detail_code == detail_code


def test_promotion_is_deterministic_and_reuses_unchanged_slice_references() -> None:
    context = _context(
        SessionCommandType.START_GAME,
        status=GameSessionStatus.CREATED,
        phase=GamePhase.LOBBY,
        materialization_cursor=4,
    )

    first = CompositeLifecycleControlApplyPlanBuilder().build(context)
    second = CompositeLifecycleControlApplyPlanBuilder().build(context)

    assert first == second
    assert isinstance(first, BuildPlanReady)
    assert isinstance(second, BuildPlanReady)
    first_candidate = first.plan.candidate_snapshot
    second_candidate = second.plan.candidate_snapshot
    assert GameSnapshotIdentity.from_snapshot(
        first_candidate
    ) == GameSnapshotIdentity.from_snapshot(second_candidate)
    current = context.session_view.current_game_snapshot
    assert current is not None
    for candidate in (first_candidate, second_candidate):
        assert candidate.setup is current.setup
        assert candidate.participants is current.participants
        assert candidate.game_rules is current.game_rules
        assert candidate.hidden_state is current.hidden_state


def test_composite_builder_is_sync_stateless_and_has_no_forbidden_dependencies() -> None:
    builder = CompositeLifecycleControlApplyPlanBuilder()
    assert builder.__slots__ == ()
    assert not inspect.iscoroutinefunction(builder.build)
    tree = ast.parse(inspect.getsource(builder_module))
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
    forbidden_roots = {
        "asyncio",
        "game_runtime.actor",
        "game_runtime.persistence",
        "game_runtime.recovery",
        "openai",
        "plugins",
        "requests",
    }
    assert all(
        not any(
            name == root or name.startswith(f"{root}.")
            for root in forbidden_roots
        )
        for name in imported
    )


@pytest.mark.parametrize(
    ("corruption", "reason", "detail_code"),
    [
        (
            "schema",
            BuildNonCommitReason.UNKNOWN_SCHEMA,
            "COMPOSITE_SNAPSHOT_SCHEMA_UNSUPPORTED",
        ),
        (
            "scope",
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "COMPOSITE_SCOPE_BINDING_MISMATCH",
        ),
        (
            "version",
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "COMPOSITE_STATE_VERSION_MISMATCH",
        ),
        (
            "cursor",
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "COMPOSITE_CURSOR_BINDING_MISMATCH",
        ),
        (
            "lifecycle",
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "COMPOSITE_LIFECYCLE_BINDING_MISMATCH",
        ),
        (
            "candidate",
            BuildNonCommitReason.INVALID_CONTEXT,
            "COMPOSITE_CANDIDATE_INVALID",
        ),
    ],
)
def test_corrupt_frozen_evidence_maps_to_closed_failure(
    corruption: str,
    reason: BuildNonCommitReason,
    detail_code: str,
) -> None:
    context = _context(
        SessionCommandType.START_GAME,
        status=GameSessionStatus.CREATED,
        phase=GamePhase.LOBBY,
    )
    current = context.session_view.current_game_snapshot
    assert current is not None
    if corruption == "schema":
        object.__setattr__(current, "snapshot_schema_version", 1)
    elif corruption == "scope":
        object.__setattr__(current, "game_id", "other-game")
    elif corruption == "version":
        object.__setattr__(current, "state_version", 3)
    elif corruption == "cursor":
        object.__setattr__(current, "last_applied_sequence_no", 7)
    elif corruption == "lifecycle":
        object.__setattr__(current, "status", GameSessionStatus.PAUSED)
    else:
        object.__setattr__(current.lifecycle, "domain_version", -2)

    outcome = CompositeLifecycleControlApplyPlanBuilder().build(context)

    assert outcome == BuildNonCommit(reason=reason, detail_code=detail_code)


def test_plan_preservation_guard_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(
        SessionCommandType.START_GAME,
        status=GameSessionStatus.CREATED,
        phase=GamePhase.LOBBY,
    )
    real_replace = dataclasses.replace

    def corrupt_plan_replace(value, **changes):
        result = real_replace(value, **changes)
        if isinstance(value, ControlApplyPlan) and "candidate_snapshot" in changes:
            object.__setattr__(
                result,
                "expected_binding_version",
                result.expected_binding_version + 1,
            )
        return result

    monkeypatch.setattr(builder_module, "replace", corrupt_plan_replace)

    outcome = CompositeLifecycleControlApplyPlanBuilder().build(context)

    assert outcome == BuildNonCommit(
        reason=BuildNonCommitReason.INVALID_CONTEXT,
        detail_code="COMPOSITE_PLAN_PRESERVATION_FAILED",
    )
