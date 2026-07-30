from __future__ import annotations

import asyncio
import ast
import inspect
from dataclasses import fields, replace
from datetime import datetime, timezone

import pytest

from game_runtime.actor.async_gate import (
    GateAdmission,
    GateLifecycle,
    SessionAsyncGate,
)
from game_runtime.actor.session_actor import GameSessionActor
from game_runtime.event import (
    EventVisibility,
    GameEvent,
    GameEventSource,
    GameEventType,
    SessionControlRejectedPayload,
    SessionPausedPayload,
)
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control import (
    ActorGameStateCompletionBoundary,
    ActorGameStateCompletionBoundaryError,
    ActorGameStateCompletionFailureReason,
    ActorGameStateControlTurnEvidenceFactory,
    ActorControlTurnValidationError,
    ActorOwnedGameStateCompletionBoundary,
    ActorVisibleGameState,
    BuildPlanReady,
    BuildReject,
    CandidateGameSnapshot,
    CommittedResultEventReference,
    CommittedControlRejectAccepted,
    ControlCompletionIdentity,
    ControlCompletionKind,
    ControlOperationStatus,
    OwnershipIntent,
    OwnershipIntentType,
    ReceiptAccepted,
    SessionCommandType,
    ControlTurnCommitReady,
    SnapshotVisibilityAccepted,
)
from game_runtime.session_control.apply_contract import (
    ControlApplyPlan,
    ControlRejectPlan,
)
from test_actor_control_turn_integration_contract import (
    RecordingProcessor,
    make_commit_ready,
    make_evidence,
    make_session,
)
from test_composite_game_snapshot_contract import _candidate

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _initial_state(
    *,
    snapshot_cursor: int = 6,
    control_cursor: int = 6,
    ownership_generation: int | None = 3,
) -> ActorVisibleGameState:
    return ActorVisibleGameState(
        snapshot=_candidate(
            dm_participant_id="participant-dm",
            status=GameSessionStatus.RUNNING,
            phase=GamePhase.EXPLORATION,
            state_version=4,
            materialization_cursor=snapshot_cursor,
        ),
        committed_control_cursor=control_cursor,
        ownership_generation=ownership_generation,
        last_completion_identity=None,
    )


def _state_after_reject() -> ActorVisibleGameState:
    return ActorVisibleGameState(
        snapshot=_initial_state().snapshot,
        committed_control_cursor=7,
        ownership_generation=3,
        last_completion_identity=ControlCompletionIdentity(
            game_id="game-1",
            session_id="session-1",
            command_id="command-7",
            operation_id="operation-7",
            input_event_id="event-7",
            input_sequence_no=7,
            state_version=4,
            commit_evidence_reference="commit-7",
            completion_kind=ControlCompletionKind.REJECTED,
        ),
    )


def _composite_apply_ready(
    current: CandidateGameSnapshot,
) -> ControlTurnCommitReady:
    ready = make_commit_ready()
    assert isinstance(ready.build_outcome, BuildPlanReady)
    legacy_candidate = ready.build_outcome.plan.candidate_snapshot
    candidate = replace(
        current,
        status=legacy_candidate.status,
        current_phase=legacy_candidate.current_phase,
        state_version=legacy_candidate.state_version,
        last_applied_sequence_no=legacy_candidate.last_applied_sequence_no,
        lifecycle=replace(
            current.lifecycle,
            domain_version=current.lifecycle.domain_version + 1,
            status=legacy_candidate.status,
        ),
    )
    outcome = replace(
        ready.build_outcome,
        plan=replace(
            ready.build_outcome.plan,
            candidate_snapshot=candidate,
        ),
    )
    return ControlTurnCommitReady(
        build_outcome=outcome,
        accepted_receipt=ready.accepted_receipt,
    )


def _result_reference(event: GameEvent, sequence: int) -> CommittedResultEventReference:
    return CommittedResultEventReference(
        event_id=event.event_id,
        sequence_no=sequence + 1,
        event_type=event.event_type,
        stored_event_reference=event.event_id,
    )


def _reject_ready_at(
    sequence: int,
    *,
    state_version: int = 4,
    ownership_generation: int | None = 3,
) -> ControlTurnCommitReady:
    command_id = f"command-{sequence}"
    operation_id = f"operation-{sequence}"
    input_event_id = f"event-{sequence}"
    claim_id = f"claim-{sequence}"
    payload = SessionControlRejectedPayload(
        command_id=command_id,
        operation_id=operation_id,
        input_event_id=input_event_id,
        result_code="STALE_VERSION",
        result_state_version=state_version,
        reason_code="STALE_VERSION",
        state_version=state_version,
    )
    event = GameEvent(
        event_id=f"event-rejected-{sequence}",
        game_id="game-1",
        session_id="session-1",
        event_type=GameEventType.SESSION_CONTROL_REJECTED,
        actor="session-actor",
        source=GameEventSource.CONTROL,
        correlation_id=f"correlation-{sequence}",
        timestamp=NOW,
        payload=payload.to_mapping(),
        visibility=EventVisibility.DM_CONTROL,
        observed_state_version=state_version,
        causation_event_id=input_event_id,
    )
    outcome = BuildReject(
        plan=ControlRejectPlan(
            game_id="game-1",
            session_id="session-1",
            group_id="group-1",
            command_id=command_id,
            command_type=SessionCommandType.PAUSE_GAME,
            operation_id=operation_id,
            operation_claim_id=claim_id,
            input_event_id=input_event_id,
            input_sequence_no=sequence,
            expected_state_version=state_version,
            expected_cursor=sequence - 1,
            expected_binding_version=2,
            rejection_event=event,
            operation_terminal_state=ControlOperationStatus.FAILED,
        )
    )
    receipt = ReceiptAccepted(
        game_id="game-1",
        session_id="session-1",
        command_id=command_id,
        operation_id=operation_id,
        claim_id=claim_id,
        input_event_id=input_event_id,
        input_sequence_no=sequence,
        committed_state_version=state_version,
        committed_cursor=sequence,
        ownership_generation=ownership_generation,
        commit_evidence_reference=f"commit-{sequence}",
        result_event_references=(_result_reference(event, sequence),),
    )
    return ControlTurnCommitReady(
        build_outcome=outcome,
        accepted_receipt=receipt,
    )


def _pause_ready_at(
    current: CandidateGameSnapshot,
    sequence: int,
) -> ControlTurnCommitReady:
    template = make_commit_ready()
    assert isinstance(template.build_outcome, BuildPlanReady)
    command_id = f"command-{sequence}"
    operation_id = f"operation-{sequence}"
    input_event_id = f"event-{sequence}"
    claim_id = f"claim-{sequence}"
    payload = SessionPausedPayload(
        command_id=command_id,
        operation_id=operation_id,
        input_event_id=input_event_id,
        result_code="APPLIED",
        result_state_version=current.state_version + 1,
        previous_status=GameSessionStatus.RUNNING,
        current_status=GameSessionStatus.PAUSED,
        reason_code="DM_REQUEST",
    )
    event = GameEvent(
        event_id=f"event-session-paused-{sequence}",
        game_id=current.game_id,
        session_id=current.session_id,
        event_type=GameEventType.SESSION_PAUSED,
        actor="session-actor",
        source=GameEventSource.CONTROL,
        correlation_id=f"correlation-{sequence}",
        timestamp=NOW,
        payload=payload.to_mapping(),
        visibility=EventVisibility.SYSTEM_ONLY,
        observed_state_version=current.state_version,
        causation_event_id=input_event_id,
    )
    candidate = replace(
        current,
        status=GameSessionStatus.PAUSED,
        state_version=current.state_version + 1,
        last_applied_sequence_no=sequence,
        lifecycle=replace(
            current.lifecycle,
            domain_version=current.lifecycle.domain_version + 1,
            status=GameSessionStatus.PAUSED,
        ),
    )
    outcome = BuildPlanReady(
        plan=ControlApplyPlan(
            game_id=current.game_id,
            session_id=current.session_id,
            group_id=current.group_id,
            command_id=command_id,
            command_type=SessionCommandType.PAUSE_GAME,
            operation_id=operation_id,
            operation_claim_id=claim_id,
            input_event_id=input_event_id,
            input_sequence_no=sequence,
            expected_state_version=current.state_version,
            expected_cursor=sequence - 1,
            expected_binding_version=2,
            candidate_snapshot=candidate,
            participant_mutations=(),
            setup_mutations=(),
            ownership_intent=OwnershipIntent(
                intent_type=OwnershipIntentType.RETAIN,
                expected_generation=3,
                resulting_generation=3,
            ),
            result_events=(event,),
            operation_terminal_state=ControlOperationStatus.SUCCESS,
            lifecycle_evidence=template.build_outcome.plan.lifecycle_evidence,
            setup_participant_evidence=(
                template.build_outcome.plan.setup_participant_evidence
            ),
        )
    )
    receipt = ReceiptAccepted(
        game_id=current.game_id,
        session_id=current.session_id,
        command_id=command_id,
        operation_id=operation_id,
        claim_id=claim_id,
        input_event_id=input_event_id,
        input_sequence_no=sequence,
        committed_state_version=candidate.state_version,
        committed_cursor=sequence,
        ownership_generation=3,
        commit_evidence_reference=f"commit-{sequence}",
        result_event_references=(_result_reference(event, sequence),),
    )
    return ControlTurnCommitReady(
        build_outcome=outcome,
        accepted_receipt=receipt,
    )


def _unsafe_replace(value: object, **changes: object) -> object:
    forged = object.__new__(type(value))
    for item in fields(value):
        object.__setattr__(
            forged,
            item.name,
            changes.get(item.name, getattr(value, item.name)),
        )
    return forged


def _with_ownership(
    ready: ControlTurnCommitReady,
    *,
    intent_type: OwnershipIntentType,
    expected: int | None,
    resulting: int | None,
) -> ControlTurnCommitReady:
    assert isinstance(ready.build_outcome, BuildPlanReady)
    outcome = replace(
        ready.build_outcome,
        plan=replace(
            ready.build_outcome.plan,
            ownership_intent=OwnershipIntent(
                intent_type=intent_type,
                expected_generation=expected,
                resulting_generation=resulting,
            ),
        ),
    )
    return ControlTurnCommitReady(
        build_outcome=outcome,
        accepted_receipt=replace(
            ready.accepted_receipt,
            ownership_generation=resulting,
        ),
    )


def test_unified_boundary_accept_is_sync_and_owns_one_visible_state() -> None:
    state = _initial_state()
    boundary = ActorOwnedGameStateCompletionBoundary(initial_state=state)

    assert not inspect.iscoroutinefunction(boundary.accept)
    assert boundary.current_state is state


def test_applied_completion_replaces_the_single_state_cell_once() -> None:
    state = _initial_state()
    boundary = ActorOwnedGameStateCompletionBoundary(initial_state=state)
    ready = _composite_apply_ready(state.snapshot)

    accepted = boundary.accept(ready)

    assert isinstance(accepted, SnapshotVisibilityAccepted)
    assert accepted.already_visible is False
    assert boundary.current_state is not state
    assert boundary.current_state.snapshot is ready.build_outcome.plan.candidate_snapshot
    assert boundary.current_state.committed_control_cursor == 7
    assert boundary.current_state.ownership_generation == 3
    completion = boundary.current_state.last_completion_identity
    assert completion is not None
    assert completion.completion_kind is ControlCompletionKind.APPLIED
    assert completion.input_sequence_no == 7
    assert completion.state_version == 5


def test_rejected_completion_advances_control_cursor_without_snapshot_change() -> None:
    state = _initial_state()
    boundary = ActorOwnedGameStateCompletionBoundary(initial_state=state)

    accepted = boundary.accept(make_commit_ready(rejected=True))

    assert isinstance(accepted, CommittedControlRejectAccepted)
    assert accepted.already_accepted is False
    assert boundary.current_state is not state
    assert boundary.current_state.snapshot is state.snapshot
    assert boundary.current_state.snapshot.state_version == 4
    assert boundary.current_state.committed_control_cursor == 7
    assert boundary.current_state.ownership_generation == 3
    completion = boundary.current_state.last_completion_identity
    assert completion is not None
    assert completion.completion_kind is ControlCompletionKind.REJECTED
    assert completion.input_sequence_no == 7
    assert completion.state_version == 4


def test_boundary_is_the_runtime_unified_completion_protocol() -> None:
    boundary = ActorOwnedGameStateCompletionBoundary(
        initial_state=_initial_state()
    )

    assert isinstance(boundary, ActorGameStateCompletionBoundary)


def test_two_rejects_then_apply_uses_control_cursor_not_snapshot_cursor() -> None:
    state = _initial_state(snapshot_cursor=6, control_cursor=6)
    boundary = ActorOwnedGameStateCompletionBoundary(initial_state=state)

    boundary.accept(_reject_ready_at(7))
    boundary.accept(_reject_ready_at(8))
    before_apply = boundary.current_state
    ready = _pause_ready_at(before_apply.snapshot, 9)
    accepted = boundary.accept(ready)

    assert isinstance(accepted, SnapshotVisibilityAccepted)
    assert accepted.previous_cursor == 8
    assert before_apply.snapshot.last_applied_sequence_no == 6
    assert boundary.current_state.snapshot.last_applied_sequence_no == 9
    assert boundary.current_state.committed_control_cursor == 9
    assert boundary.current_state.snapshot.state_version == 5


@pytest.mark.parametrize(
    ("initial_generation", "intent_type", "expected", "resulting"),
    [
        (None, OwnershipIntentType.ACQUIRE, None, 4),
        (3, OwnershipIntentType.RETAIN, 3, 3),
        (3, OwnershipIntentType.RELEASE, 3, None),
    ],
)
def test_applied_completion_uses_committed_ownership_intent(
    initial_generation: int | None,
    intent_type: OwnershipIntentType,
    expected: int | None,
    resulting: int | None,
) -> None:
    state = _initial_state(ownership_generation=initial_generation)
    ready = _with_ownership(
        _pause_ready_at(state.snapshot, 7),
        intent_type=intent_type,
        expected=expected,
        resulting=resulting,
    )
    boundary = ActorOwnedGameStateCompletionBoundary(initial_state=state)

    accepted = boundary.accept(ready)

    assert isinstance(accepted, SnapshotVisibilityAccepted)
    assert accepted.ownership_generation == resulting
    assert boundary.current_state.ownership_generation == resulting


@pytest.mark.parametrize("rejected", [False, True])
def test_exact_last_completion_duplicate_does_not_replace_state(
    rejected: bool,
) -> None:
    state = _initial_state()
    boundary = ActorOwnedGameStateCompletionBoundary(initial_state=state)
    ready = (
        _reject_ready_at(7)
        if rejected
        else _pause_ready_at(state.snapshot, 7)
    )
    first = boundary.accept(ready)
    installed = boundary.current_state

    duplicate = boundary.accept(ready)

    assert boundary.current_state is installed
    if rejected:
        assert isinstance(first, CommittedControlRejectAccepted)
        assert isinstance(duplicate, CommittedControlRejectAccepted)
        assert duplicate.already_accepted is True
    else:
        assert isinstance(first, SnapshotVisibilityAccepted)
        assert isinstance(duplicate, SnapshotVisibilityAccepted)
        assert duplicate.already_visible is True


def test_conflicting_duplicate_fails_closed_without_replacing_state() -> None:
    state = _initial_state()
    boundary = ActorOwnedGameStateCompletionBoundary(initial_state=state)
    ready = _pause_ready_at(state.snapshot, 7)
    boundary.accept(ready)
    installed = boundary.current_state
    conflict = ControlTurnCommitReady(
        build_outcome=ready.build_outcome,
        accepted_receipt=replace(
            ready.accepted_receipt,
            commit_evidence_reference="different-commit",
        ),
    )

    with pytest.raises(ActorGameStateCompletionBoundaryError) as captured:
        boundary.accept(conflict)

    assert (
        captured.value.reason
        is ActorGameStateCompletionFailureReason.COMPLETION_IDENTITY_CONFLICT
    )
    assert boundary.current_state is installed


@pytest.mark.parametrize(
    ("state", "ready", "reason"),
    [
        (
            _state_after_reject(),
            _pause_ready_at(_initial_state().snapshot, 9),
            ActorGameStateCompletionFailureReason.CONTROL_CURSOR_MISMATCH,
        ),
        (
            ActorVisibleGameState(
                snapshot=_candidate(
                    dm_participant_id="participant-dm",
                    status=GameSessionStatus.RUNNING,
                    phase=GamePhase.EXPLORATION,
                    state_version=3,
                    materialization_cursor=6,
                ),
                committed_control_cursor=6,
                ownership_generation=3,
                last_completion_identity=None,
            ),
            _pause_ready_at(_initial_state().snapshot, 7),
            ActorGameStateCompletionFailureReason.VERSION_MISMATCH,
        ),
    ],
)
def test_committed_state_mismatch_fails_closed_without_visibility(
    state: ActorVisibleGameState,
    ready: ControlTurnCommitReady,
    reason: ActorGameStateCompletionFailureReason,
) -> None:
    boundary = ActorOwnedGameStateCompletionBoundary(initial_state=state)

    with pytest.raises(ActorGameStateCompletionBoundaryError) as captured:
        boundary.accept(ready)

    assert captured.value.reason is reason
    assert boundary.current_state is state


def test_reject_ownership_mismatch_fails_closed() -> None:
    state = _initial_state()
    boundary = ActorOwnedGameStateCompletionBoundary(initial_state=state)

    with pytest.raises(ActorGameStateCompletionBoundaryError) as captured:
        boundary.accept(_reject_ready_at(7, ownership_generation=4))

    assert (
        captured.value.reason
        is ActorGameStateCompletionFailureReason.OWNERSHIP_MISMATCH
    )
    assert boundary.current_state is state


def test_raw_commit_value_is_rejected_without_state_change() -> None:
    state = _initial_state()
    boundary = ActorOwnedGameStateCompletionBoundary(initial_state=state)

    with pytest.raises(ActorGameStateCompletionBoundaryError) as captured:
        boundary.accept(object())  # type: ignore[arg-type]

    assert (
        captured.value.reason
        is ActorGameStateCompletionFailureReason.INVALID_COMMIT_READY
    )
    assert boundary.current_state is state


def test_legacy_candidate_cannot_enter_composite_visibility() -> None:
    state = _initial_state()
    boundary = ActorOwnedGameStateCompletionBoundary(initial_state=state)

    with pytest.raises(ActorGameStateCompletionBoundaryError) as captured:
        boundary.accept(make_commit_ready())

    assert (
        captured.value.reason
        is ActorGameStateCompletionFailureReason.SNAPSHOT_IDENTITY_MISMATCH
    )
    assert boundary.current_state is state


def test_forged_receipt_operation_identity_is_rejected() -> None:
    state = _initial_state()
    boundary = ActorOwnedGameStateCompletionBoundary(initial_state=state)
    ready = _pause_ready_at(state.snapshot, 7)
    receipt = _unsafe_replace(
        ready.accepted_receipt,
        command_id="other-command",
    )
    forged = _unsafe_replace(ready, accepted_receipt=receipt)

    with pytest.raises(ActorGameStateCompletionBoundaryError) as captured:
        boundary.accept(forged)  # type: ignore[arg-type]

    assert (
        captured.value.reason
        is ActorGameStateCompletionFailureReason.OPERATION_IDENTITY_MISMATCH
    )
    assert boundary.current_state is state


def test_forged_candidate_materialization_cursor_is_rejected() -> None:
    state = _initial_state()
    boundary = ActorOwnedGameStateCompletionBoundary(initial_state=state)
    ready = _pause_ready_at(state.snapshot, 7)
    plan = ready.build_outcome.plan
    candidate = replace(
        plan.candidate_snapshot,
        last_applied_sequence_no=8,
    )
    forged_plan = _unsafe_replace(plan, candidate_snapshot=candidate)
    forged_outcome = _unsafe_replace(ready.build_outcome, plan=forged_plan)
    forged = _unsafe_replace(ready, build_outcome=forged_outcome)

    with pytest.raises(ActorGameStateCompletionBoundaryError) as captured:
        boundary.accept(forged)  # type: ignore[arg-type]

    assert (
        captured.value.reason
        is ActorGameStateCompletionFailureReason.MATERIALIZATION_CURSOR_MISMATCH
    )
    assert boundary.current_state is state


def test_forged_plan_group_scope_is_rejected() -> None:
    state = _initial_state()
    boundary = ActorOwnedGameStateCompletionBoundary(initial_state=state)
    ready = _pause_ready_at(state.snapshot, 7)
    plan = _unsafe_replace(
        ready.build_outcome.plan,
        group_id="other-group",
    )
    outcome = _unsafe_replace(ready.build_outcome, plan=plan)
    forged = _unsafe_replace(ready, build_outcome=outcome)

    with pytest.raises(ActorGameStateCompletionBoundaryError) as captured:
        boundary.accept(forged)  # type: ignore[arg-type]

    assert (
        captured.value.reason
        is ActorGameStateCompletionFailureReason.SCOPE_MISMATCH
    )
    assert boundary.current_state is state


def test_forged_raw_receipt_is_rejected_as_invalid_proof() -> None:
    state = _initial_state()
    boundary = ActorOwnedGameStateCompletionBoundary(initial_state=state)
    ready = _pause_ready_at(state.snapshot, 7)
    forged = _unsafe_replace(ready, accepted_receipt=object())

    with pytest.raises(ActorGameStateCompletionBoundaryError) as captured:
        boundary.accept(forged)  # type: ignore[arg-type]

    assert (
        captured.value.reason
        is ActorGameStateCompletionFailureReason.RECEIPT_BINDING_MISMATCH
    )
    assert boundary.current_state is state


def test_forged_rejection_reason_binding_is_rejected() -> None:
    state = _initial_state()
    boundary = ActorOwnedGameStateCompletionBoundary(initial_state=state)
    ready = _reject_ready_at(7)
    plan = ready.build_outcome.plan
    payload = dict(plan.rejection_event.payload)
    payload["result_code"] = "OTHER_REASON"
    event = replace(plan.rejection_event, payload=payload)
    forged_plan = _unsafe_replace(plan, rejection_event=event)
    forged_outcome = _unsafe_replace(ready.build_outcome, plan=forged_plan)
    forged = _unsafe_replace(ready, build_outcome=forged_outcome)

    with pytest.raises(ActorGameStateCompletionBoundaryError) as captured:
        boundary.accept(forged)  # type: ignore[arg-type]

    assert (
        captured.value.reason
        is ActorGameStateCompletionFailureReason.RECEIPT_BINDING_MISMATCH
    )
    assert boundary.current_state is state


class RecordingGameStateEvidenceFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[ActorVisibleGameState, object]] = []
        self.evidence = make_evidence()

    def build(self, *, state: ActorVisibleGameState, envelope: object):
        self.calls.append((state, envelope))
        self.evidence = replace(
            self.evidence,
            session_view=replace(
                self.evidence.session_view,
                status=state.snapshot.status,
                current_phase=state.snapshot.current_phase,
                state_version=state.snapshot.state_version,
                last_applied_sequence_no=state.committed_control_cursor,
                current_game_snapshot=state.snapshot,
            ),
            ownership_evidence=replace(
                self.evidence.ownership_evidence,
                active_generation=state.ownership_generation,
                observed_state_version=state.snapshot.state_version,
            ),
        )
        return self.evidence


def test_composite_actor_uses_exact_state_for_evidence_and_publishes_success() -> None:
    initial = _initial_state()
    ready = _composite_apply_ready(initial.snapshot)
    factory = RecordingGameStateEvidenceFactory()
    actor = GameSessionActor.for_composite_control(
        session_projection_seed=make_session(),
        initial_state=initial,
        control_turn_evidence_factory=factory,
        control_turn_processor=RecordingProcessor(ready),
    )

    assert isinstance(factory, ActorGameStateControlTurnEvidenceFactory)
    assert actor.visible_game_state is initial

    assert asyncio.run(actor.handle_control_turn(factory.evidence.envelope)) is None

    assert factory.calls == [(initial, factory.evidence.envelope)]
    assert actor.visible_game_state.snapshot is ready.build_outcome.plan.candidate_snapshot
    assert actor.visible_game_state.committed_control_cursor == 7


def test_composite_actor_session_is_a_non_authoritative_fresh_projection() -> None:
    initial = _initial_state(snapshot_cursor=6, control_cursor=6)
    actor = GameSessionActor.for_composite_control(
        session_projection_seed=make_session(),
        initial_state=initial,
        control_turn_evidence_factory=RecordingGameStateEvidenceFactory(),
        control_turn_processor=RecordingProcessor(
            _composite_apply_ready(initial.snapshot)
        ),
    )

    first = actor.session
    second = actor.session
    first.state_version = 999
    first.last_applied_sequence_no = 999

    assert first is not second
    assert actor.visible_game_state is initial
    assert second.state_version == initial.snapshot.state_version
    assert second.last_applied_sequence_no == initial.committed_control_cursor


def test_composite_actor_routes_reject_into_the_same_state_cell() -> None:
    initial = _initial_state()
    factory = RecordingGameStateEvidenceFactory()
    actor = GameSessionActor.for_composite_control(
        session_projection_seed=make_session(),
        initial_state=initial,
        control_turn_evidence_factory=factory,
        control_turn_processor=RecordingProcessor(
            make_commit_ready(rejected=True)
        ),
    )

    asyncio.run(actor.handle_control_turn(factory.evidence.envelope))

    assert actor.visible_game_state.snapshot is initial.snapshot
    assert actor.visible_game_state.committed_control_cursor == 7
    assert (
        actor.visible_game_state.last_completion_identity.completion_kind
        is ControlCompletionKind.REJECTED
    )


def test_composite_actor_rejects_evidence_not_bound_to_exact_state_cell() -> None:
    initial = _initial_state()

    class StaleFactory:
        def build(self, *, state: ActorVisibleGameState, envelope: object):
            return make_evidence()

    evidence = make_evidence()
    actor = GameSessionActor.for_composite_control(
        session_projection_seed=make_session(),
        initial_state=initial,
        control_turn_evidence_factory=StaleFactory(),
        control_turn_processor=RecordingProcessor(
            _composite_apply_ready(initial.snapshot)
        ),
    )

    with pytest.raises(ActorControlTurnValidationError):
        asyncio.run(actor.handle_control_turn(evidence.envelope))

    assert actor.visible_game_state is initial


def test_composite_actor_rejects_projection_seed_scope_mismatch() -> None:
    seed = make_session()
    seed.group_id = "other-group"

    with pytest.raises(ActorControlTurnValidationError):
        GameSessionActor.for_composite_control(
            session_projection_seed=seed,
            initial_state=_initial_state(),
            control_turn_evidence_factory=RecordingGameStateEvidenceFactory(),
            control_turn_processor=RecordingProcessor(
                _composite_apply_ready(_initial_state().snapshot)
            ),
        )


def test_legacy_actor_keeps_existing_session_path_without_composite_state() -> None:
    seed = make_session()
    actor = GameSessionActor(seed)

    assert actor.session is seed
    with pytest.raises(ActorControlTurnValidationError):
        _ = actor.visible_game_state


def test_composite_contract_modules_have_no_forbidden_runtime_dependencies() -> None:
    import game_runtime.session_control.game_state_completion as module

    tree = ast.parse(inspect.getsource(module))
    imports = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    imports.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert not any(
        forbidden in imported
        for imported in imports
        for forbidden in (
            "persistence",
            "event_store",
            "recovery",
            "notification",
            "plugin",
            "agent_tools",
            "openai",
        )
    )
    assert calls.isdisjoint({"commit", "append", "coordinate", "create_task"})
    assert not any(
        isinstance(node, (ast.AsyncFunctionDef, ast.Await))
        for node in ast.walk(tree)
    )


def test_gate_consumes_composite_actor_without_a_second_queue() -> None:
    initial = _initial_state()
    ready = _composite_apply_ready(initial.snapshot)

    async def scenario() -> tuple[GateLifecycle, GameSessionActor]:
        factory = RecordingGameStateEvidenceFactory()
        actor = GameSessionActor.for_composite_control(
            session_projection_seed=make_session(),
            initial_state=initial,
            control_turn_evidence_factory=factory,
            control_turn_processor=RecordingProcessor(ready),
        )
        gate = SessionAsyncGate(
            game_id="game-1",
            session_id="session-1",
            next_expected_sequence_no=7,
            consumer=actor,
            max_queue_size=1,
        )
        await gate.start()
        assert await gate.admit(factory.evidence.envelope) is GateAdmission.ACCEPTED
        await gate.close()
        return gate.lifecycle, actor

    lifecycle, actor = asyncio.run(scenario())

    assert lifecycle is GateLifecycle.CLOSED
    assert actor.visible_game_state.committed_control_cursor == 7
    assert not any(isinstance(value, asyncio.Queue) for value in vars(actor).values())


def test_composite_evidence_failure_fault_stops_existing_gate() -> None:
    initial = _initial_state()
    ready = _composite_apply_ready(initial.snapshot)

    async def scenario() -> tuple[SessionAsyncGate, GameSessionActor]:
        evidence = make_evidence()

        class StaleFactory:
            def build(self, *, state: ActorVisibleGameState, envelope: object):
                return evidence

        actor = GameSessionActor.for_composite_control(
            session_projection_seed=make_session(),
            initial_state=initial,
            control_turn_evidence_factory=StaleFactory(),
            control_turn_processor=RecordingProcessor(ready),
        )
        gate = SessionAsyncGate(
            game_id="game-1",
            session_id="session-1",
            next_expected_sequence_no=7,
            consumer=actor,
            max_queue_size=1,
        )
        await gate.start()
        assert await gate.admit(evidence.envelope) is GateAdmission.ACCEPTED
        await gate.wait_faulted()
        return gate, actor

    gate, actor = asyncio.run(scenario())

    assert gate.lifecycle is GateLifecycle.FAULTED
    assert isinstance(gate.fault, ActorControlTurnValidationError)
    assert actor.visible_game_state is not None
    assert actor.visible_game_state.committed_control_cursor == 6
