from __future__ import annotations

import ast
import asyncio
from dataclasses import FrozenInstanceError, fields, replace
import inspect

import pytest

from game_runtime.actor.session_actor import GameSessionActor
from game_runtime.actor.async_gate import GateAdmission, GateLifecycle, SessionAsyncGate
from game_runtime.event import GameEventType
from game_runtime.session_control import (
    ActorOwnedApplyCoordinator,
    ActorOwnedControlRejectCompletionBoundary,
    ActorControlTurnValidationError,
    BuildNonCommit,
    BuildNonCommitReason,
    BuildPlanReady,
    CandidateSessionSnapshot,
    CommittedResultEventReference,
    CommittedControlRejectAccepted,
    ControlApplyReceipt,
    ControlApplyConflictReason,
    ControlOperationClaim,
    ControlRejectCompletionBoundaryError,
    ControlRejectCompletionFailureReason,
    ControlTurnProcessor,
    ControlTurnAcceptance,
    LifecycleControlApplyPlanBuilder,
    OwnershipIntentType,
    CoordinatorApplyConflict,
    CoordinatorApplyUnknown,
    CoordinatorApplyUnknownReason,
    CoordinatorClaimConflict,
    CoordinatorClaimUnknown,
    CoordinatorClaimUnknownReason,
    CoordinatorNonCommit,
    SessionCommandType,
    StartReadinessStatus,
)
from game_runtime.session_control.apply_plan_builder import BuildReject
from game_runtime.session_control.coordinator_evidence import (
    ActorValidatedControlTurnEvidence,
    ControlClaimAttemptEvidence,
)
from game_runtime.session_control.control_turn_contract import ControlTurnCommitReady
from game_runtime.session_control.lifecycle_snapshot_boundary import (
    ActorOwnedLifecycleSnapshotCommitBoundary,
    SnapshotVisibilityAccepted,
)
from game_runtime.session_control.operation import ControlOperationStatus
from game_runtime.session_control.setup_participant_evidence import (
    ControlSetupParticipantApplyEvidence,
)
from game_runtime.session import GamePhase, GameSessionStatus
from test_actor_control_turn_integration_contract import (
    FixedCoordinator,
    RecordingEvidenceFactory,
    RecordingProcessor,
    make_commit_ready,
    make_build_outcome,
    make_claim,
    make_evidence,
    make_handoff,
    make_session,
)
from test_lifecycle_phase_apply_plan_builder import make_context


def make_reject_boundary(
    *,
    state_version: int = 4,
    cursor: int = 6,
) -> ActorOwnedControlRejectCompletionBoundary:
    return ActorOwnedControlRejectCompletionBoundary(
        game_id="game-1",
        session_id="session-1",
        current_state_version=state_version,
        current_cursor=cursor,
    )


def forged_commit_ready(
    ready: ControlTurnCommitReady,
    *,
    build_outcome: object | None = None,
    accepted_receipt: object | None = None,
) -> ControlTurnCommitReady:
    forged = object.__new__(ControlTurnCommitReady)
    object.__setattr__(
        forged,
        "build_outcome",
        ready.build_outcome if build_outcome is None else build_outcome,
    )
    object.__setattr__(
        forged,
        "accepted_receipt",
        ready.accepted_receipt if accepted_receipt is None else accepted_receipt,
    )
    return forged


def forged_receipt(ready: ControlTurnCommitReady, **changes: object) -> object:
    receipt = ready.accepted_receipt
    forged = object.__new__(type(receipt))
    for item in fields(receipt):
        object.__setattr__(
            forged,
            item.name,
            changes.get(item.name, getattr(receipt, item.name)),
        )
    return forged


def assert_reject_failure(
    boundary: ActorOwnedControlRejectCompletionBoundary,
    value: object,
    reason: ControlRejectCompletionFailureReason,
) -> None:
    with pytest.raises(ControlRejectCompletionBoundaryError) as captured:
        boundary.accept(value)  # type: ignore[arg-type]
    assert captured.value.reason is reason


def test_committed_reject_acceptance_is_immutable_slotted_and_value_only() -> None:
    ready = make_commit_ready(rejected=True)
    accepted = make_reject_boundary().accept(ready)

    assert isinstance(accepted, CommittedControlRejectAccepted)
    assert not hasattr(accepted, "__dict__")
    assert {item.name for item in fields(accepted)} == {
        "game_id",
        "session_id",
        "command_id",
        "operation_id",
        "claim_id",
        "input_event_id",
        "input_sequence_no",
        "unchanged_state_version",
        "previous_cursor",
        "committed_cursor",
        "rejection_reason",
        "committed_rejection_event_reference",
        "commit_evidence_reference",
        "already_accepted",
    }
    with pytest.raises(FrozenInstanceError):
        accepted.already_accepted = True  # type: ignore[misc]
    forbidden = ("snapshot", "session", "actor", "port", "callback", "task")
    assert {item.name for item in fields(accepted)}.isdisjoint(forbidden)
    for item in fields(accepted):
        contract = str(item.type).lower()
        assert not any(term in contract for term in forbidden)


def test_reject_boundary_accepts_only_committed_build_reject() -> None:
    boundary = make_reject_boundary()
    ready = make_commit_ready(rejected=True)

    assert not inspect.iscoroutinefunction(boundary.accept)
    accepted = boundary.accept(ready)

    assert isinstance(accepted, CommittedControlRejectAccepted)
    assert accepted.already_accepted is False
    assert accepted.unchanged_state_version == 4
    assert accepted.previous_cursor == 6
    assert accepted.committed_cursor == accepted.input_sequence_no == 7
    assert accepted.rejection_reason == "STALE_VERSION"
    assert accepted.committed_rejection_event_reference == "event-rejected-1"
    assert accepted.commit_evidence_reference == "commit-evidence-1"
    assert boundary.current_state_version == 4
    assert boundary.current_cursor == 7


def test_reject_boundary_rejects_raw_receipt_and_build_plan_ready() -> None:
    boundary = make_reject_boundary()
    success = make_commit_ready()

    assert_reject_failure(
        boundary,
        success.accepted_receipt,
        ControlRejectCompletionFailureReason.INVALID_COMMIT_READY,
    )
    assert_reject_failure(
        boundary,
        success,
        ControlRejectCompletionFailureReason.BUILD_PLAN_NOT_REJECT,
    )


def test_reject_completion_exact_duplicate_is_idempotent() -> None:
    boundary = make_reject_boundary()
    ready = make_commit_ready(rejected=True)

    first = boundary.accept(ready)
    second = boundary.accept(ready)

    assert first.already_accepted is False
    assert second == replace(first, already_accepted=True)
    assert boundary.current_cursor == 7


@pytest.mark.parametrize(
    ("receipt_change", "reason"),
    [
        (
            {"operation_id": "other-operation"},
            ControlRejectCompletionFailureReason.IDENTITY_MISMATCH,
        ),
        (
            {"committed_state_version": 5},
            ControlRejectCompletionFailureReason.VERSION_MISMATCH,
        ),
        (
            {"committed_cursor": 8},
            ControlRejectCompletionFailureReason.CURSOR_MISMATCH,
        ),
        (
            {
                "result_event_references": (
                    CommittedResultEventReference(
                        event_id="other-rejection-event",
                        sequence_no=8,
                        event_type=GameEventType.SESSION_CONTROL_REJECTED,
                        stored_event_reference="other-rejection-event",
                    ),
                )
            },
            ControlRejectCompletionFailureReason.RESULT_EVENT_MISMATCH,
        ),
        (
            {
                "result_event_references": (
                    CommittedResultEventReference(
                        event_id="event-rejected-1",
                        sequence_no=7,
                        event_type=GameEventType.SESSION_CONTROL_REJECTED,
                        stored_event_reference="event-rejected-1",
                    ),
                )
            },
            ControlRejectCompletionFailureReason.RESULT_EVENT_MISMATCH,
        ),
        (
            {"commit_evidence_reference": "other-commit"},
            ControlRejectCompletionFailureReason.ALREADY_ACCEPTED_CONFLICT,
        ),
    ],
)
def test_reject_completion_mismatch_and_conflicting_duplicate_fail_closed(
    receipt_change: dict[str, object],
    reason: ControlRejectCompletionFailureReason,
) -> None:
    ready = make_commit_ready(rejected=True)
    boundary = make_reject_boundary()
    if reason is ControlRejectCompletionFailureReason.ALREADY_ACCEPTED_CONFLICT:
        boundary.accept(ready)
    forged = forged_commit_ready(
        ready,
        accepted_receipt=forged_receipt(ready, **receipt_change),
    )

    assert_reject_failure(boundary, forged, reason)


class RecordingSnapshotBoundary:
    def __init__(self) -> None:
        self.calls: list[ControlTurnCommitReady] = []
        self.delegate = ActorOwnedLifecycleSnapshotCommitBoundary(
            current_snapshot=make_commit_ready().build_outcome.plan.candidate_snapshot,
            ownership_generation=3,
        )

    def accept(self, ready: ControlTurnCommitReady) -> SnapshotVisibilityAccepted:
        self.calls.append(ready)
        return self.delegate.accept(ready)


class RecordingRejectBoundary:
    def __init__(self) -> None:
        self.calls: list[ControlTurnCommitReady] = []
        self.delegate = make_reject_boundary()

    def accept(self, ready: ControlTurnCommitReady) -> CommittedControlRejectAccepted:
        self.calls.append(ready)
        return self.delegate.accept(ready)


def test_actor_routes_committed_business_reject_without_snapshot_replacement() -> None:
    evidence = make_evidence()
    ready = make_commit_ready(rejected=True)
    snapshot = RecordingSnapshotBoundary()
    reject = RecordingRejectBoundary()
    actor = GameSessionActor(
        make_session(),
        control_turn_evidence_factory=RecordingEvidenceFactory(evidence),
        control_turn_processor=RecordingProcessor(ready),
        control_commit_boundary=snapshot,
        control_reject_completion_boundary=reject,
    )

    assert asyncio.run(actor.handle_control_turn(evidence.envelope)) is None

    assert snapshot.calls == []
    assert reject.calls == [ready]
    assert reject.delegate.current_cursor == ready.accepted_receipt.committed_cursor


def test_control_turn_acceptance_union_distinguishes_both_terminal_outcomes() -> None:
    assert ControlTurnAcceptance == (
        SnapshotVisibilityAccepted | CommittedControlRejectAccepted
    )


def test_reject_completion_has_no_forbidden_runtime_dependencies() -> None:
    module = inspect.getmodule(ActorOwnedControlRejectCompletionBoundary)
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
            "commit_control_apply",
            "commit_control_rejection",
            "append_event",
            "send",
            "recover",
            "retry",
            "transition_to",
            "transition_phase_to",
        }
    )


class ContractApplyPort:
    """No-I/O Atomic Commit contract double used only by vertical tests."""

    def __init__(self, *, ownership_generation: int | None) -> None:
        self.ownership_generation = ownership_generation
        self.calls: list[tuple[str, object]] = []
        self.trace: list[str] = []

    async def claim_operation(self, **values: object) -> ControlOperationClaim:
        self.calls.append(("claim_operation", dict(values)))
        self.trace.append("claim")
        return ControlOperationClaim(**values)  # type: ignore[arg-type]

    async def commit_control_apply(
        self,
        plan: object,
        claim: ControlOperationClaim,
    ) -> ControlApplyReceipt:
        self.calls.append(("commit_control_apply", plan))
        self.trace.append("commit")
        return self._receipt(
            plan=plan,
            claim=claim,
            events=plan.result_events,
            state_version=plan.candidate_snapshot.state_version,
            ownership_generation=plan.ownership_intent.resulting_generation,
            status=ControlOperationStatus.SUCCESS,
        )

    async def commit_control_rejection(
        self,
        plan: object,
        claim: ControlOperationClaim,
    ) -> ControlApplyReceipt:
        self.calls.append(("commit_control_rejection", plan))
        self.trace.append("commit_rejection")
        return self._receipt(
            plan=plan,
            claim=claim,
            events=(plan.rejection_event,),
            state_version=plan.expected_state_version,
            ownership_generation=self.ownership_generation,
            status=ControlOperationStatus.FAILED,
        )

    @staticmethod
    def _receipt(
        *,
        plan: object,
        claim: ControlOperationClaim,
        events: tuple[object, ...],
        state_version: int,
        ownership_generation: int | None,
        status: ControlOperationStatus,
    ) -> ControlApplyReceipt:
        references = tuple(
            CommittedResultEventReference(
                event_id=event.event_id,
                sequence_no=plan.input_sequence_no + ordinal,
                event_type=event.event_type,
                stored_event_reference=event.event_id,
            )
            for ordinal, event in enumerate(events, start=1)
        )
        return ControlApplyReceipt(
            game_id=plan.game_id,
            session_id=plan.session_id,
            committed_state_version=state_version,
            committed_cursor=plan.input_sequence_no,
            result_event_ids=tuple(event.event_id for event in events),
            operation_status=status,
            ownership_generation=ownership_generation,
            commit_evidence_reference=f"commit:{plan.operation_id}",
            command_id=plan.command_id,
            operation_id=plan.operation_id,
            operation_claim_id=claim.claim_id,
            input_event_id=plan.input_event_id,
            input_sequence_no=plan.input_sequence_no,
            result_event_references=references,
        )


class RecordingLifecycleVisibilityBoundary:
    def __init__(
        self,
        *,
        current_snapshot: CandidateSessionSnapshot,
        ownership_generation: int | None,
        trace: list[str],
    ) -> None:
        self.calls: list[ControlTurnCommitReady] = []
        self.acceptance: SnapshotVisibilityAccepted | None = None
        self.trace = trace
        self.delegate = ActorOwnedLifecycleSnapshotCommitBoundary(
            current_snapshot=current_snapshot,
            ownership_generation=ownership_generation,
        )

    def accept(self, ready: ControlTurnCommitReady) -> SnapshotVisibilityAccepted:
        self.calls.append(ready)
        self.trace.append("visibility")
        self.acceptance = self.delegate.accept(ready)
        return self.acceptance


class RecordingCommittedRejectBoundary:
    def __init__(
        self,
        *,
        state_version: int,
        cursor: int,
        trace: list[str],
    ) -> None:
        self.calls: list[ControlTurnCommitReady] = []
        self.acceptance: CommittedControlRejectAccepted | None = None
        self.trace = trace
        self.delegate = ActorOwnedControlRejectCompletionBoundary(
            game_id="game-1",
            session_id="session-1",
            current_state_version=state_version,
            current_cursor=cursor,
        )

    def accept(
        self,
        ready: ControlTurnCommitReady,
    ) -> CommittedControlRejectAccepted:
        self.calls.append(ready)
        self.trace.append("reject_completion")
        self.acceptance = self.delegate.accept(ready)
        return self.acceptance


def evidence_from_context(context: object) -> ActorValidatedControlTurnEvidence:
    envelope = context.envelope
    return ActorValidatedControlTurnEvidence(
        envelope=envelope,
        command_intent=context.command_intent,
        session_view=context.session_view,
        participant_views=context.participant_views,
        setup_view=context.setup_view,
        ownership_evidence=context.ownership_evidence,
        governance_schema_version=1,
        authorization_reference=envelope.authorization_reference,
        confirmation_reference=envelope.confirmation_reference,
        lifecycle_evidence=context.lifecycle_evidence,
        setup_participant_evidence=ControlSetupParticipantApplyEvidence(),
        claim_attempt=ControlClaimAttemptEvidence.from_envelope(
            envelope,
            claimed_at=context.claim.claimed_at,
        ),
    )


def snapshot_from_context(context: object) -> CandidateSessionSnapshot:
    view = context.session_view
    return CandidateSessionSnapshot(
        game_id=view.game_id,
        session_id=view.session_id,
        group_id=view.group_id,
        dm_participant_id=view.dm_participant_id,
        status=view.status,
        current_phase=view.current_phase,
        state_version=view.state_version,
        last_applied_sequence_no=view.last_applied_sequence_no,
    )


def run_vertical_turn(context: object):
    evidence = evidence_from_context(context)
    port = ContractApplyPort(
        ownership_generation=context.ownership_evidence.active_generation
    )
    coordinator = ActorOwnedApplyCoordinator(
        apply_port=port,
        builder=LifecycleControlApplyPlanBuilder(),
    )
    snapshot_boundary = RecordingLifecycleVisibilityBoundary(
        current_snapshot=snapshot_from_context(context),
        ownership_generation=context.ownership_evidence.active_generation,
        trace=port.trace,
    )
    reject_boundary = RecordingCommittedRejectBoundary(
        state_version=context.session_view.state_version,
        cursor=context.session_view.last_applied_sequence_no,
        trace=port.trace,
    )
    actor = GameSessionActor(
        make_session(),
        control_turn_evidence_factory=RecordingEvidenceFactory(evidence),
        control_turn_processor=ControlTurnProcessor(coordinator),
        control_commit_boundary=snapshot_boundary,
        control_reject_completion_boundary=reject_boundary,
    )

    async def scenario() -> GateLifecycle:
        gate = SessionAsyncGate(
            game_id="game-1",
            session_id="session-1",
            next_expected_sequence_no=context.envelope.event_sequence_no,
            consumer=actor,
            max_queue_size=1,
        )
        await gate.start()
        assert await gate.admit(context.envelope) is GateAdmission.ACCEPTED
        await gate.close()
        return gate.lifecycle

    lifecycle = asyncio.run(scenario())
    return port, snapshot_boundary, reject_boundary, lifecycle


@pytest.mark.parametrize(
    (
        "command_type",
        "status",
        "phase",
        "expected_status",
        "expected_phase",
        "expected_ownership",
        "expected_events",
    ),
    [
        (
            "START_GAME",
            GameSessionStatus.CREATED,
            GamePhase.LOBBY,
            GameSessionStatus.RUNNING,
            GamePhase.INTRODUCTION,
            OwnershipIntentType.ACQUIRE,
            (GameEventType.SESSION_STARTED, GameEventType.PHASE_CHANGED),
        ),
        (
            "PAUSE_GAME",
            GameSessionStatus.RUNNING,
            GamePhase.EXPLORATION,
            GameSessionStatus.PAUSED,
            GamePhase.EXPLORATION,
            OwnershipIntentType.RETAIN,
            (GameEventType.SESSION_PAUSED,),
        ),
        (
            "END_GAME",
            GameSessionStatus.CREATED,
            GamePhase.LOBBY,
            GameSessionStatus.ENDED,
            GamePhase.ENDING,
            OwnershipIntentType.UNCHANGED,
            (GameEventType.SESSION_ENDED,),
        ),
        (
            "END_GAME",
            GameSessionStatus.RUNNING,
            GamePhase.DISCUSSION,
            GameSessionStatus.ENDED,
            GamePhase.ENDING,
            OwnershipIntentType.RELEASE,
            (GameEventType.SESSION_ENDED,),
        ),
        (
            "END_GAME",
            GameSessionStatus.PAUSED,
            GamePhase.DISCUSSION,
            GameSessionStatus.ENDED,
            GamePhase.ENDING,
            OwnershipIntentType.RELEASE,
            (GameEventType.SESSION_ENDED,),
        ),
    ],
)
def test_start_pause_end_full_flow_commits_before_snapshot_visibility(
    command_type: str,
    status: GameSessionStatus,
    phase: GamePhase,
    expected_status: GameSessionStatus,
    expected_phase: GamePhase,
    expected_ownership: OwnershipIntentType,
    expected_events: tuple[GameEventType, ...],
) -> None:
    context = make_context(
        SessionCommandType(command_type),
        status=status,
        current_phase=phase,
    )
    port, snapshot_boundary, reject_boundary, lifecycle = run_vertical_turn(context)

    assert lifecycle is GateLifecycle.CLOSED
    assert [name for name, _ in port.calls] == [
        "claim_operation",
        "commit_control_apply",
    ]
    assert port.trace == ["claim", "commit", "visibility"]
    assert len(snapshot_boundary.calls) == 1
    assert reject_boundary.calls == []
    ready = snapshot_boundary.calls[0]
    assert isinstance(ready.build_outcome, BuildPlanReady)
    plan = ready.build_outcome.plan
    assert plan.ownership_intent.intent_type is expected_ownership
    assert tuple(event.event_type for event in plan.result_events) == expected_events
    assert plan.candidate_snapshot.status is expected_status
    assert plan.candidate_snapshot.current_phase is expected_phase
    assert plan.candidate_snapshot.state_version == context.session_view.state_version + 1
    assert (
        plan.candidate_snapshot.last_applied_sequence_no
        == context.session_view.last_applied_sequence_no + 1
    )
    assert snapshot_boundary.delegate.current_snapshot is plan.candidate_snapshot
    assert isinstance(snapshot_boundary.acceptance, SnapshotVisibilityAccepted)


def not_ready_start_context() -> object:
    context = make_context(
        SessionCommandType.START_GAME,
        status=GameSessionStatus.CREATED,
        current_phase=GamePhase.LOBBY,
    )
    readiness = replace(
        context.lifecycle_evidence.start_readiness,
        readiness_status=StartReadinessStatus.NOT_READY,
    )
    return replace(
        context,
        lifecycle_evidence=replace(
            context.lifecycle_evidence,
            start_readiness=readiness,
        ),
    )


@pytest.mark.parametrize(
    ("context_factory", "expected_reason"),
    [
        (
            lambda: make_context(
                SessionCommandType.START_GAME,
                status=GameSessionStatus.RUNNING,
                current_phase=GamePhase.EXPLORATION,
            ),
            "INVALID_LIFECYCLE_TRANSITION",
        ),
        (not_ready_start_context, "START_NOT_READY"),
        (
            lambda: make_context(
                SessionCommandType.PAUSE_GAME,
                status=GameSessionStatus.PAUSED,
                current_phase=GamePhase.EXPLORATION,
            ),
            "INVALID_LIFECYCLE_TRANSITION",
        ),
        (
            lambda: make_context(
                SessionCommandType.END_GAME,
                status=GameSessionStatus.ENDED,
                current_phase=GamePhase.ENDING,
            ),
            "SESSION_ALREADY_ENDED",
        ),
    ],
)
def test_business_reject_full_flow_uses_reject_completion_without_snapshot(
    context_factory: object,
    expected_reason: str,
) -> None:
    context = context_factory()
    before = snapshot_from_context(context)
    port, snapshot_boundary, reject_boundary, lifecycle = run_vertical_turn(context)

    assert lifecycle is GateLifecycle.CLOSED
    assert [name for name, _ in port.calls] == [
        "claim_operation",
        "commit_control_rejection",
    ]
    assert port.trace == ["claim", "commit_rejection", "reject_completion"]
    assert snapshot_boundary.calls == []
    assert snapshot_boundary.delegate.current_snapshot == before
    assert len(reject_boundary.calls) == 1
    ready = reject_boundary.calls[0]
    assert isinstance(ready.build_outcome, BuildReject)
    assert ready.accepted_receipt.committed_state_version == before.state_version
    assert reject_boundary.delegate.current_state_version == before.state_version
    assert reject_boundary.delegate.current_cursor == context.envelope.event_sequence_no
    assert reject_boundary.acceptance is not None
    assert reject_boundary.acceptance.rejection_reason == expected_reason


def test_stale_version_rejection_contract_handoff_completes_through_gate() -> None:
    evidence = make_evidence()
    coordinator = FixedCoordinator(make_handoff(rejected=True))
    snapshot = RecordingSnapshotBoundary()
    reject = RecordingRejectBoundary()
    actor = GameSessionActor(
        make_session(),
        control_turn_evidence_factory=RecordingEvidenceFactory(evidence),
        control_turn_processor=ControlTurnProcessor(coordinator),
        control_commit_boundary=snapshot,
        control_reject_completion_boundary=reject,
    )

    async def scenario() -> GateLifecycle:
        gate = SessionAsyncGate(
            game_id="game-1",
            session_id="session-1",
            next_expected_sequence_no=7,
            consumer=actor,
            max_queue_size=1,
        )
        await gate.start()
        assert await gate.admit(evidence.envelope) is GateAdmission.ACCEPTED
        assert await gate.admit(evidence.envelope) is GateAdmission.DUPLICATE
        await gate.close()
        return gate.lifecycle

    assert asyncio.run(scenario()) is GateLifecycle.CLOSED
    assert len(coordinator.calls) == 1
    assert snapshot.calls == []
    assert len(reject.calls) == 1
    accepted = reject.delegate.accept(make_commit_ready(rejected=True))
    assert accepted.already_accepted is True
    assert accepted.rejection_reason == "STALE_VERSION"


def test_reject_completion_mismatch_propagates_and_fault_stops_gate() -> None:
    evidence = make_evidence()
    ready = make_commit_ready(rejected=True)
    snapshot = RecordingSnapshotBoundary()
    reject = RecordingCommittedRejectBoundary(
        state_version=4,
        cursor=5,
        trace=[],
    )
    processor = RecordingProcessor(ready)
    actor = GameSessionActor(
        make_session(),
        control_turn_evidence_factory=RecordingEvidenceFactory(evidence),
        control_turn_processor=processor,
        control_commit_boundary=snapshot,
        control_reject_completion_boundary=reject,
    )

    async def scenario() -> SessionAsyncGate:
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
        return gate

    gate = asyncio.run(scenario())
    assert gate.lifecycle is GateLifecycle.FAULTED
    assert isinstance(gate.fault, ControlRejectCompletionBoundaryError)
    assert gate.fault.reason is ControlRejectCompletionFailureReason.CURSOR_MISMATCH
    assert len(processor.calls) == 1
    assert snapshot.calls == []
    assert len(reject.calls) == 1
    assert reject.acceptance is None


def test_unknown_commit_ready_outcome_is_typed_fail_closed() -> None:
    evidence = make_evidence()
    valid = make_commit_ready()
    forged = forged_commit_ready(valid, build_outcome=object())
    snapshot = RecordingSnapshotBoundary()
    reject = RecordingRejectBoundary()
    actor = GameSessionActor(
        make_session(),
        control_turn_evidence_factory=RecordingEvidenceFactory(evidence),
        control_turn_processor=RecordingProcessor(forged),
        control_commit_boundary=snapshot,
        control_reject_completion_boundary=reject,
    )

    async def scenario() -> SessionAsyncGate:
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
        return gate

    gate = asyncio.run(scenario())
    assert gate.lifecycle is GateLifecycle.FAULTED
    assert isinstance(gate.fault, ActorControlTurnValidationError)
    assert snapshot.calls == []
    assert reject.calls == []


@pytest.mark.parametrize(
    "coordinator_outcome",
    [
        CoordinatorClaimConflict(
            reason=ControlApplyConflictReason.OPERATION_CLAIM_MISMATCH
        ),
        CoordinatorClaimUnknown(
            reason=CoordinatorClaimUnknownReason.STORAGE_FAILURE
        ),
        CoordinatorNonCommit(
            claim=make_claim(),
            outcome=BuildNonCommit(
                reason=BuildNonCommitReason.EVIDENCE_MISMATCH,
                detail_code="E2E_EVIDENCE_MISMATCH",
            ),
        ),
        CoordinatorApplyConflict(
            claim=make_claim(),
            build_outcome=make_build_outcome(),
            reason=ControlApplyConflictReason.STATE_VERSION_MISMATCH,
        ),
        CoordinatorApplyUnknown(
            claim=make_claim(),
            build_outcome=make_build_outcome(),
            reason=CoordinatorApplyUnknownReason.RECEIPT_MISSING,
        ),
        replace(
            make_handoff(),
            receipt=replace(make_handoff().receipt, committed_cursor=8),
        ),
    ],
)
def test_coordinator_and_receipt_failures_fault_stop_without_acceptance(
    coordinator_outcome: object,
) -> None:
    evidence = make_evidence()
    coordinator = FixedCoordinator(coordinator_outcome)
    snapshot = RecordingSnapshotBoundary()
    reject = RecordingRejectBoundary()
    actor = GameSessionActor(
        make_session(),
        control_turn_evidence_factory=RecordingEvidenceFactory(evidence),
        control_turn_processor=ControlTurnProcessor(coordinator),
        control_commit_boundary=snapshot,
        control_reject_completion_boundary=reject,
    )

    async def scenario() -> SessionAsyncGate:
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
        return gate

    gate = asyncio.run(scenario())
    assert gate.lifecycle is GateLifecycle.FAULTED
    assert len(coordinator.calls) == 1
    assert snapshot.calls == []
    assert reject.calls == []


def test_snapshot_invariant_failure_fault_stops_without_reject_acceptance() -> None:
    evidence = make_evidence()
    ready = make_commit_ready()
    snapshot = RecordingLifecycleVisibilityBoundary(
        current_snapshot=replace(
            ready.build_outcome.plan.candidate_snapshot,
            state_version=3,
            last_applied_sequence_no=6,
            status=GameSessionStatus.RUNNING,
        ),
        ownership_generation=3,
        trace=[],
    )
    reject = RecordingRejectBoundary()
    actor = GameSessionActor(
        make_session(),
        control_turn_evidence_factory=RecordingEvidenceFactory(evidence),
        control_turn_processor=RecordingProcessor(ready),
        control_commit_boundary=snapshot,
        control_reject_completion_boundary=reject,
    )

    async def scenario() -> SessionAsyncGate:
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
        return gate

    gate = asyncio.run(scenario())
    assert gate.lifecycle is GateLifecycle.FAULTED
    assert len(snapshot.calls) == 1
    assert snapshot.acceptance is None
    assert reject.calls == []
