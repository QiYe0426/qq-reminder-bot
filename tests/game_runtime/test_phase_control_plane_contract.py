from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import replace
import inspect

import pytest

import game_runtime.session_control.phase_control_builder as builder_module
from game_runtime.event import (
    GameEventSource,
    GameEventType,
    PhaseChangedPayload,
    SessionControlRejectedPayload,
    validate_control_result_event,
)
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control import (
    BuildNonCommit,
    BuildNonCommitReason,
    BuildPlanReady,
    BuildReject,
    CandidateGameSnapshot,
    ControlLifecycleApplyEvidence,
    ControlOwnershipBuildEvidence,
    LifecycleSnapshotSlice,
    OwnershipIntentType,
    PauseGamePayload,
    PhaseSnapshotSlice,
    SessionCommandType,
)
from game_runtime.session_control.operation import ControlOperationStatus
from game_runtime.session_control.phase_control_builder import (
    PhaseControlApplyPlanBuilder,
)
from test_composite_lifecycle_promotion_contract import _current_snapshot
from test_lifecycle_phase_apply_plan_builder import (
    _end_evidence,
    _phase_evidence,
    make_context,
)


LEGAL_PHASE_EDGES = (
    (GamePhase.INTRODUCTION, GamePhase.EXPLORATION),
    (GamePhase.EXPLORATION, GamePhase.DISCUSSION),
    (GamePhase.DISCUSSION, GamePhase.EXPLORATION),
    (GamePhase.DISCUSSION, GamePhase.VOTING),
    (GamePhase.VOTING, GamePhase.DISCUSSION),
    (GamePhase.VOTING, GamePhase.ENDING),
)


def _context(
    previous_phase: GamePhase = GamePhase.EXPLORATION,
    target_phase: GamePhase = GamePhase.DISCUSSION,
    *,
    status: GameSessionStatus = GameSessionStatus.RUNNING,
    session_version: int = 4,
    command_observed_version: int | None = None,
    active_generation: int | None = 3,
):
    context = make_context(
        SessionCommandType.CHANGE_PHASE,
        status=status,
        current_phase=previous_phase,
        target_phase=target_phase,
        session_version=session_version,
        command_observed_version=command_observed_version,
        active_generation=active_generation,
    )
    return replace(
        context,
        session_view=replace(
            context.session_view,
            current_game_snapshot=_current_snapshot(context),
        ),
    )


@pytest.mark.parametrize(("previous_phase", "target_phase"), LEGAL_PHASE_EDGES)
def test_all_legal_edges_build_composite_phase_apply_plan(
    previous_phase: GamePhase,
    target_phase: GamePhase,
) -> None:
    context = _context(previous_phase, target_phase)

    outcome = PhaseControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildPlanReady)
    plan = outcome.plan
    candidate = plan.candidate_snapshot
    assert isinstance(candidate, CandidateGameSnapshot)
    assert candidate.status is GameSessionStatus.RUNNING
    assert candidate.current_phase is target_phase
    assert plan.ownership_intent.intent_type is OwnershipIntentType.RETAIN
    assert plan.ownership_intent.expected_generation == 3
    assert plan.ownership_intent.resulting_generation == 3


def test_success_builds_complete_candidate_and_reuses_non_phase_slices() -> None:
    context = _context()
    current = context.session_view.current_game_snapshot
    assert current is not None

    outcome = PhaseControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildPlanReady)
    plan = outcome.plan
    candidate = plan.candidate_snapshot
    assert isinstance(candidate, CandidateGameSnapshot)
    assert (
        candidate.game_id,
        candidate.session_id,
        candidate.group_id,
        candidate.dm_participant_id,
    ) == (
        current.game_id,
        current.session_id,
        current.group_id,
        current.dm_participant_id,
    )
    assert candidate.snapshot_schema_version == current.snapshot_schema_version
    assert candidate.status is current.status
    assert candidate.lifecycle is current.lifecycle
    assert candidate.setup is current.setup
    assert candidate.participants is current.participants
    assert candidate.game_rules is current.game_rules
    assert candidate.hidden_state is current.hidden_state
    assert candidate.phase is not current.phase
    assert candidate.phase.schema_version == current.phase.schema_version
    assert candidate.phase.domain_version == current.phase.domain_version + 1
    assert candidate.phase.phase is GamePhase.DISCUSSION
    assert candidate.state_version == current.state_version + 1
    assert (
        candidate.last_applied_sequence_no
        == context.envelope.event_sequence_no
    )
    assert plan.expected_state_version == current.state_version
    assert plan.expected_cursor == context.session_view.last_applied_sequence_no
    assert plan.participant_mutations == ()
    assert plan.setup_mutations == ()
    assert plan.lifecycle_evidence is context.lifecycle_evidence
    assert (
        plan.setup_participant_evidence
        is context.setup_participant_evidence
    )
    assert plan.operation_terminal_state is ControlOperationStatus.SUCCESS


def test_success_emits_exact_phase_changed_event_with_all_bindings() -> None:
    context = _context()

    outcome = PhaseControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildPlanReady)
    plan = outcome.plan
    assert len(plan.result_events) == 1
    event = plan.result_events[0]
    assert event.event_type is GameEventType.PHASE_CHANGED
    assert event.event_id == context.result_event_seed.derive_event_id(
        GameEventType.PHASE_CHANGED,
        ordinal=1,
    )
    assert (event.game_id, event.session_id) == (
        context.session_view.game_id,
        context.session_view.session_id,
    )
    assert event.source is GameEventSource.CONTROL
    assert event.correlation_id == context.result_event_seed.correlation_id
    assert event.timestamp == context.result_event_seed.timestamp
    assert event.causation_event_id == context.envelope.event.event_id
    assert event.observed_state_version == context.session_view.state_version
    payload = validate_control_result_event(event)
    assert isinstance(payload, PhaseChangedPayload)
    assert payload.command_id == context.envelope.command_id
    assert payload.operation_id == context.envelope.operation_id
    assert payload.input_event_id == context.envelope.event.event_id
    assert payload.result_code == "CHANGE_PHASE_APPLIED"
    assert payload.result_state_version == context.session_view.state_version + 1
    assert payload.previous_phase is GamePhase.EXPLORATION
    assert payload.current_phase is GamePhase.DISCUSSION


@pytest.mark.parametrize(
    ("context", "reason"),
    [
        (
            _context(
                session_version=5,
                command_observed_version=4,
            ),
            "STALE_STATE_VERSION",
        ),
        (
            _context(status=GameSessionStatus.PAUSED),
            "INVALID_LIFECYCLE_TRANSITION",
        ),
        (
            _context(target_phase=GamePhase.VOTING),
            "INVALID_PHASE_TRANSITION",
        ),
    ],
)
def test_business_failures_build_reject_without_candidate_or_version_advance(
    context,
    reason: str,
) -> None:
    outcome = PhaseControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildReject)
    plan = outcome.plan
    assert not hasattr(plan, "candidate_snapshot")
    assert plan.expected_state_version == context.session_view.state_version
    assert plan.expected_cursor == context.session_view.last_applied_sequence_no
    assert plan.operation_terminal_state is ControlOperationStatus.FAILED
    event = plan.rejection_event
    assert event.event_type is GameEventType.SESSION_CONTROL_REJECTED
    assert event.observed_state_version == context.session_view.state_version
    payload = validate_control_result_event(event)
    assert isinstance(payload, SessionControlRejectedPayload)
    assert payload.reason_code == reason
    assert payload.result_code == reason
    assert payload.result_state_version == context.session_view.state_version
    assert payload.state_version == context.session_view.state_version


def test_command_version_ahead_returns_noncommit() -> None:
    context = _context(
        session_version=4,
        command_observed_version=5,
    )

    outcome = PhaseControlApplyPlanBuilder().build(context)

    assert outcome == BuildNonCommit(
        reason=BuildNonCommitReason.EVIDENCE_MISMATCH,
        detail_code="COMMAND_VERSION_AHEAD_OF_ACTOR",
    )


def test_nonrunning_lifecycle_rejects_before_exact_phase_evidence() -> None:
    context = _context(status=GameSessionStatus.PAUSED)
    object.__setattr__(
        context,
        "lifecycle_evidence",
        ControlLifecycleApplyEvidence(),
    )

    outcome = PhaseControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildReject)
    payload = validate_control_result_event(outcome.plan.rejection_event)
    assert isinstance(payload, SessionControlRejectedPayload)
    assert payload.reason_code == "INVALID_LIFECYCLE_TRANSITION"


def test_unsafe_command_intent_container_returns_noncommit() -> None:
    context = _context()
    object.__setattr__(context, "command_intent", object())

    outcome = PhaseControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildNonCommit)
    assert not hasattr(outcome, "plan")


def test_unsafe_command_type_returns_invalid_context_noncommit() -> None:
    context = _context()
    object.__setattr__(context.command_intent, "command_type", object())

    outcome = PhaseControlApplyPlanBuilder().build(context)

    assert outcome == BuildNonCommit(
        reason=BuildNonCommitReason.INVALID_CONTEXT,
        detail_code="CONTROL_COMMAND_TYPE_INVALID",
    )
    assert not hasattr(outcome, "plan")


@pytest.mark.parametrize(
    ("owner_name", "field_name"),
    [
        ("intent", "intent_schema_version"),
        ("intent", "command_type"),
        ("intent", "payload"),
        ("payload", "target_phase"),
    ],
)
def test_deleted_command_intent_structure_returns_noncommit(
    owner_name: str,
    field_name: str,
) -> None:
    context = _context()
    owner = (
        context.command_intent
        if owner_name == "intent"
        else context.command_intent.payload
    )
    object.__delattr__(owner, field_name)

    outcome = PhaseControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildNonCommit)
    assert not hasattr(outcome, "plan")


@pytest.mark.parametrize(
    "field_name",
    ["lifecycle_evidence", "ownership_evidence"],
)
def test_nonrunning_with_unsafe_consumed_container_returns_noncommit(
    field_name: str,
) -> None:
    context = _context(status=GameSessionStatus.PAUSED)
    object.__setattr__(context, field_name, object())

    outcome = PhaseControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildNonCommit)
    assert not hasattr(outcome, "plan")


@pytest.mark.parametrize(
    "corruption",
    [
        "lifecycle_schema",
        "phase_domain",
        "setup_schema",
        "participants_schema",
        "participant_record",
        "game_rules_schema",
        "hidden_state_schema",
    ],
)
def test_revalidates_current_snapshot_and_every_nested_contract(
    corruption: str,
) -> None:
    context = _context()
    current = context.session_view.current_game_snapshot
    assert current is not None
    if corruption == "lifecycle_schema":
        object.__setattr__(current.lifecycle, "schema_version", 2)
    elif corruption == "phase_domain":
        object.__setattr__(current.phase, "domain_version", -1)
    elif corruption == "setup_schema":
        object.__setattr__(current.setup, "schema_version", 2)
    elif corruption == "participants_schema":
        object.__setattr__(current.participants, "schema_version", 2)
    elif corruption == "participant_record":
        object.__setattr__(
            current.participants.participants[0],
            "binding_version",
            -1,
        )
    elif corruption == "game_rules_schema":
        object.__setattr__(current.game_rules, "schema_version", 3)
    else:
        object.__setattr__(current.hidden_state, "schema_version", 2)

    outcome = PhaseControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildNonCommit)
    assert not hasattr(outcome, "plan")


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("game_id", "other-game"),
        ("session_id", "other-session"),
        ("group_id", "other-group"),
        ("observed_state_version", 3),
        ("last_allocated_generation", 2),
    ],
)
def test_revalidates_ownership_contract_and_actor_turn_bindings(
    field_name: str,
    value: object,
) -> None:
    context = _context()
    object.__setattr__(context.ownership_evidence, field_name, value)

    outcome = PhaseControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildNonCommit)
    assert not hasattr(outcome, "plan")


@pytest.mark.parametrize("corruption", ["envelope_version", "seed_timestamp"])
def test_bad_consumed_nested_context_contract_never_leaks_exception(
    corruption: str,
) -> None:
    context = _context()
    if corruption == "envelope_version":
        object.__setattr__(
            context.envelope,
            "observed_state_version",
            "4",
        )
    else:
        object.__setattr__(context.result_event_seed, "timestamp", "bad")

    outcome = PhaseControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildNonCommit)
    assert not hasattr(outcome, "plan")


def test_revalidates_phase_visibility_intent_contract() -> None:
    context = _context()
    visibility = context.lifecycle_evidence.phase_visibility_intent
    assert visibility is not None
    object.__setattr__(visibility, "contract_version", 2)

    outcome = PhaseControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildNonCommit)
    assert not hasattr(outcome, "plan")


@pytest.mark.parametrize(
    ("corruption", "detail_code"),
    [
        ("evidence_missing", "CHANGE_PHASE_EVIDENCE_INCOMPLETE"),
        ("evidence_extra", "CHANGE_PHASE_EVIDENCE_INCOMPLETE"),
        (
            "visibility_previous",
            "CHANGE_PHASE_VISIBILITY_BINDING_MISMATCH",
        ),
        ("visibility_target", "CHANGE_PHASE_VISIBILITY_BINDING_MISMATCH"),
        ("ownership_missing", "CHANGE_PHASE_OWNERSHIP_MISSING"),
        ("ownership_nonpositive", "CHANGE_PHASE_OWNERSHIP_INVALID"),
        (
            "participant",
            "COMPOSITE_PARTICIPANT_BINDING_MISMATCH",
        ),
        ("setup", "COMPOSITE_SETUP_BINDING_MISMATCH"),
        ("scope", "COMPOSITE_SCOPE_BINDING_MISMATCH"),
        ("version", "COMPOSITE_STATE_VERSION_MISMATCH"),
        ("cursor", "COMPOSITE_CURSOR_BINDING_MISMATCH"),
        ("lifecycle", "COMPOSITE_LIFECYCLE_BINDING_MISMATCH"),
        ("candidate", "COMPOSITE_CANDIDATE_INVALID"),
    ],
)
def test_evidence_ownership_and_composite_corruption_fail_closed(
    corruption: str,
    detail_code: str,
) -> None:
    context = _context()
    current = context.session_view.current_game_snapshot
    assert current is not None
    if corruption == "evidence_missing":
        object.__setattr__(
            context,
            "lifecycle_evidence",
            ControlLifecycleApplyEvidence(),
        )
    elif corruption == "evidence_extra":
        object.__setattr__(
            context,
            "lifecycle_evidence",
            ControlLifecycleApplyEvidence(
                phase_visibility_intent=context.lifecycle_evidence.phase_visibility_intent,
                end_retention=_end_evidence(4),
            ),
        )
    elif corruption in {"visibility_previous", "visibility_target"}:
        visibility = context.lifecycle_evidence.phase_visibility_intent
        assert visibility is not None
        changed = (
            replace(visibility, previous_phase=GamePhase.INTRODUCTION)
            if corruption == "visibility_previous"
            else replace(visibility, target_phase=GamePhase.VOTING)
        )
        object.__setattr__(
            context,
            "lifecycle_evidence",
            ControlLifecycleApplyEvidence(phase_visibility_intent=changed),
        )
    elif corruption == "ownership_missing":
        object.__setattr__(
            context,
            "ownership_evidence",
            replace(context.ownership_evidence, active_generation=None),
        )
    elif corruption == "ownership_nonpositive":
        object.__setattr__(context.ownership_evidence, "active_generation", 0)
    elif corruption == "participant":
        record = current.participants.participants[0]
        object.__setattr__(
            current.participants,
            "participants",
            (replace(record, binding_version=record.binding_version + 1),),
        )
    elif corruption == "setup":
        object.__setattr__(current.setup, "public_name", "Other Script")
    elif corruption == "scope":
        object.__setattr__(current, "game_id", "other-game")
    elif corruption == "version":
        object.__setattr__(current, "state_version", 3)
    elif corruption == "cursor":
        object.__setattr__(current, "last_applied_sequence_no", 7)
    elif corruption == "lifecycle":
        object.__setattr__(
            current,
            "lifecycle",
            LifecycleSnapshotSlice(
                schema_version=current.lifecycle.schema_version,
                domain_version=current.lifecycle.domain_version,
                status=GameSessionStatus.PAUSED,
            ),
        )
    else:
        object.__setattr__(current.phase, "schema_version", 2)

    outcome = PhaseControlApplyPlanBuilder().build(context)

    assert outcome == BuildNonCommit(
        reason=BuildNonCommitReason.EVIDENCE_MISMATCH
        if corruption != "candidate"
        else BuildNonCommitReason.INVALID_CONTEXT,
        detail_code=detail_code,
    )
    assert not hasattr(outcome, "plan")


@pytest.mark.parametrize(
    ("malformation", "reason", "detail_code"),
    [
        (
            "context_type",
            BuildNonCommitReason.INVALID_CONTEXT,
            "CONTROL_APPLY_CONTEXT_TYPE_INVALID",
        ),
        (
            "intent_schema",
            BuildNonCommitReason.UNKNOWN_SCHEMA,
            "CONTROL_COMMAND_INTENT_SCHEMA_UNSUPPORTED",
        ),
        (
            "snapshot_missing",
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "COMPOSITE_CURRENT_SNAPSHOT_MISSING",
        ),
        (
            "snapshot_schema",
            BuildNonCommitReason.UNKNOWN_SCHEMA,
            "COMPOSITE_SNAPSHOT_SCHEMA_UNSUPPORTED",
        ),
        (
            "unsupported_command",
            BuildNonCommitReason.REDUCER_UNAVAILABLE,
            "PAUSE_GAME_REDUCER_UNAVAILABLE",
        ),
        (
            "payload_type",
            BuildNonCommitReason.INVALID_CONTEXT,
            "CHANGE_PHASE_PAYLOAD_INVALID",
        ),
        (
            "payload_value",
            BuildNonCommitReason.INVALID_CONTEXT,
            "CHANGE_PHASE_PAYLOAD_INVALID",
        ),
        (
            "lifecycle_evidence_type",
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "CHANGE_PHASE_EVIDENCE_INCOMPLETE",
        ),
        (
            "snapshot_composition",
            BuildNonCommitReason.INVALID_CONTEXT,
            "COMPOSITE_CURRENT_SNAPSHOT_INVALID",
        ),
        (
            "ownership_type",
            BuildNonCommitReason.INVALID_CONTEXT,
            "CHANGE_PHASE_OWNERSHIP_INVALID",
        ),
    ],
)
def test_malformed_or_unsupported_input_returns_closed_noncommit(
    malformation: str,
    reason: BuildNonCommitReason,
    detail_code: str,
) -> None:
    context = _context()
    if malformation == "context_type":
        value = object()
    else:
        value = context
        current = context.session_view.current_game_snapshot
        assert current is not None
        if malformation == "intent_schema":
            object.__setattr__(
                context.command_intent,
                "intent_schema_version",
                2,
            )
        elif malformation == "snapshot_missing":
            object.__setattr__(
                context.session_view,
                "current_game_snapshot",
                None,
            )
        elif malformation == "snapshot_schema":
            object.__setattr__(current, "snapshot_schema_version", 4)
        elif malformation == "unsupported_command":
            object.__setattr__(
                context.command_intent,
                "command_type",
                SessionCommandType.PAUSE_GAME,
            )
        elif malformation == "payload_type":
            object.__setattr__(
                context.command_intent,
                "payload",
                PauseGamePayload(reason_code="INVALID"),
            )
        elif malformation == "payload_value":
            object.__setattr__(
                context.command_intent.payload,
                "target_phase",
                "DISCUSSION",
            )
        elif malformation == "lifecycle_evidence_type":
            object.__setattr__(context, "lifecycle_evidence", object())
        elif malformation == "snapshot_composition":
            object.__setattr__(current, "phase", object())
        else:
            object.__setattr__(context, "ownership_evidence", object())

    outcome = PhaseControlApplyPlanBuilder().build(value)  # type: ignore[arg-type]

    assert outcome == BuildNonCommit(reason=reason, detail_code=detail_code)
    assert not hasattr(outcome, "plan")


def test_builder_is_sync_stateless_deterministic_pure_and_exported() -> None:
    from game_runtime.session_control import (
        PhaseControlApplyPlanBuilder as ExportedBuilder,
    )

    context = _context()
    before = deepcopy(context)
    builder = PhaseControlApplyPlanBuilder()

    first = builder.build(context)
    second = builder.build(context)

    assert ExportedBuilder is PhaseControlApplyPlanBuilder
    assert builder.__slots__ == ()
    assert not hasattr(builder, "__dict__")
    assert not inspect.iscoroutinefunction(builder.build)
    assert first == second
    assert isinstance(first, BuildPlanReady)
    assert isinstance(second, BuildPlanReady)
    assert first.plan is not second.plan
    assert context == before
    assert (
        first.plan.result_events[0].event_id
        == second.plan.result_events[0].event_id
    )


def test_builder_directly_uses_phase_transition_without_forbidden_dependencies() -> None:
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
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
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
    forbidden_modules = {
        "asyncio",
        "game_runtime.session_control.composite_lifecycle_builder",
        "game_runtime.session_control.lifecycle_phase_builder",
        "openai",
        "requests",
    }
    forbidden_calls = {
        "CompositeLifecycleControlApplyPlanBuilder",
        "LifecyclePhaseControlApplyPlanBuilder",
        "apply",
        "commit",
        "dispatch",
        "notify",
        "recover",
        "retry",
    }

    assert "transition_phase" in called_names
    assert imported.isdisjoint(forbidden_modules)
    assert all(
        not any(term in module.casefold() for term in forbidden_terms)
        for module in imported
    )
    assert called_names.isdisjoint(forbidden_calls)
