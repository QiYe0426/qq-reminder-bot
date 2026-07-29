from __future__ import annotations

import ast
import asyncio
from collections import deque
from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timezone
import inspect
from pathlib import Path

import pytest

from game_runtime.actor.async_gate import (
    GateAdmission,
    GateLifecycle,
    GateNotAcceptingError,
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
from game_runtime.identity import DMIdentity
from game_runtime.participant import ParticipantMembershipState, ParticipantType
from game_runtime.session import GamePhase, GameSession, GameSessionStatus
from game_runtime.session_control.apply_contract import (
    CandidateSessionSnapshot,
    CommittedResultEventReference,
    ControlApplyConflictReason,
    ControlApplyPlan,
    ControlApplyReceipt,
    ControlOperationClaim,
    ControlRejectPlan,
    OwnershipIntent,
    OwnershipIntentType,
)
from game_runtime.session_control.apply_coordinator import (
    CoordinatorApplyUnknown,
    CoordinatorApplyUnknownReason,
    CoordinatorClaimConflict,
)
from game_runtime.session_control.apply_plan_builder import (
    BuildPlanReady,
    BuildReject,
)
from game_runtime.session_control.build_context import (
    CanonicalControlCommandIntent,
    ControlOwnershipBuildEvidence,
    ControlParticipantBuildView,
    ControlSessionBuildView,
    ControlSetupBuildView,
)
from game_runtime.session_control.commands import PauseGamePayload, SessionCommandType
from game_runtime.session_control.confirmation import fingerprint_payload
from game_runtime.session_control.control_turn_contract import (
    ActorControlCommitBoundary,
    ActorControlRejectCompletionBoundary,
    ActorControlTurnError,
    ActorControlTurnEvidenceFactory,
    ActorControlTurnProcessor,
    ControlTurnCommitReady,
    ControlTurnFailureReason,
    ControlTurnProcessingError,
)
from game_runtime.session_control.control_reject_completion import (
    ActorOwnedControlRejectCompletionBoundary,
    CommittedControlRejectAccepted,
)
from game_runtime.session_control.lifecycle_snapshot_boundary import (
    ActorOwnedLifecycleSnapshotCommitBoundary,
    SnapshotVisibilityAccepted,
)
from game_runtime.session_control.control_turn_processor import ControlTurnProcessor
from game_runtime.session_control.coordinator_evidence import (
    ActorValidatedControlTurnEvidence,
    ControlClaimAttemptEvidence,
    CoordinatorCommitReturned,
)
from game_runtime.session_control.delivery import ControlEventDeliveryEnvelope
from game_runtime.session_control.lifecycle_evidence import ControlLifecycleApplyEvidence
from game_runtime.session_control.operation import ControlOperationStatus
from game_runtime.session_control.receipt_validation import ReceiptAccepted
from game_runtime.session_control.setup_participant_evidence import (
    ControlSetupParticipantApplyEvidence,
)


NOW = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)


def make_envelope() -> ControlEventDeliveryEnvelope:
    payload = PauseGamePayload(reason_code="DM_REQUEST")
    payload_fingerprint = fingerprint_payload(payload)
    event = GameEvent(
        event_id="event-7",
        game_id="game-1",
        session_id="session-1",
        event_type=GameEventType.DM_COMMAND,
        actor="participant-dm",
        source=GameEventSource.CONTROL,
        correlation_id="correlation-7",
        timestamp=NOW,
        payload={
            "command_id": "command-7",
            "command_type": SessionCommandType.PAUSE_GAME.value,
            "requester": "participant-dm",
            "causation_event_id": "request-event-7",
            "observed_state_version": 4,
            "payload_reference": f"sha256:{payload_fingerprint}",
            "payload_fingerprint": payload_fingerprint,
            "visibility": EventVisibility.DM_CONTROL.value,
        },
        visibility=EventVisibility.DM_CONTROL,
        observed_state_version=4,
        causation_event_id="request-event-7",
    )
    return ControlEventDeliveryEnvelope(
        event=event,
        event_sequence_no=7,
        operation_id="operation-7",
        command_id="command-7",
        observed_state_version=4,
        requester_principal_ref="participant-dm",
        requester_binding_version=2,
        authorization_reference="authorization-7",
        confirmation_reference="confirmation-7",
        correlation_id="correlation-7",
        stored_event_reference="event-7",
    )


def make_evidence() -> ActorValidatedControlTurnEvidence:
    envelope = make_envelope()
    payload = PauseGamePayload(reason_code="DM_REQUEST")
    return ActorValidatedControlTurnEvidence(
        envelope=envelope,
        command_intent=CanonicalControlCommandIntent(
            intent_schema_version=1,
            command_type=SessionCommandType.PAUSE_GAME,
            payload=payload,
            payload_fingerprint=fingerprint_payload(payload),
        ),
        session_view=ControlSessionBuildView(
            game_id="game-1",
            session_id="session-1",
            group_id="group-1",
            dm_participant_id="participant-dm",
            status=GameSessionStatus.RUNNING,
            current_phase=GamePhase.EXPLORATION,
            state_version=4,
            last_applied_sequence_no=6,
        ),
        participant_views=(
            ControlParticipantBuildView(
                game_id="game-1",
                session_id="session-1",
                participant_id="participant-dm",
                participant_type=ParticipantType.DM,
                membership_state=ParticipantMembershipState.ACTIVE,
                character_id=None,
                binding_version=2,
            ),
        ),
        setup_view=ControlSetupBuildView(
            game_id="game-1",
            session_id="session-1",
            script_id="script-1",
            public_name="Public Script",
            manifest_reference="manifest-1",
            setup_version=1,
        ),
        ownership_evidence=ControlOwnershipBuildEvidence(
            game_id="game-1",
            session_id="session-1",
            group_id="group-1",
            active_generation=3,
            last_allocated_generation=3,
            observed_state_version=4,
        ),
        governance_schema_version=1,
        authorization_reference="authorization-7",
        confirmation_reference="confirmation-7",
        lifecycle_evidence=ControlLifecycleApplyEvidence(),
        setup_participant_evidence=ControlSetupParticipantApplyEvidence(),
        claim_attempt=ControlClaimAttemptEvidence.from_envelope(
            envelope,
            claimed_at=NOW,
        ),
    )


def make_claim() -> ControlOperationClaim:
    attempt = make_evidence().claim_attempt
    return ControlOperationClaim(
        game_id=attempt.game_id,
        session_id=attempt.session_id,
        command_id=attempt.command_id,
        operation_id=attempt.operation_id,
        input_event_id=attempt.input_event_id,
        claim_id=attempt.claim_id,
        claimed_at=attempt.claimed_at,
    )


def make_result_event(*, rejected: bool = False) -> GameEvent:
    if rejected:
        payload = SessionControlRejectedPayload(
            command_id="command-7",
            operation_id="operation-7",
            input_event_id="event-7",
            result_code="STALE_VERSION",
            result_state_version=4,
            reason_code="STALE_VERSION",
            state_version=4,
        )
        event_type = GameEventType.SESSION_CONTROL_REJECTED
        visibility = EventVisibility.DM_CONTROL
        event_id = "event-rejected-1"
    else:
        payload = SessionPausedPayload(
            command_id="command-7",
            operation_id="operation-7",
            input_event_id="event-7",
            result_code="APPLIED",
            result_state_version=5,
            previous_status=GameSessionStatus.RUNNING,
            current_status=GameSessionStatus.PAUSED,
            reason_code="DM_REQUEST",
        )
        event_type = GameEventType.SESSION_PAUSED
        visibility = EventVisibility.SYSTEM_ONLY
        event_id = "event-session-paused"
    return GameEvent(
        event_id=event_id,
        game_id="game-1",
        session_id="session-1",
        event_type=event_type,
        actor="session-actor",
        source=GameEventSource.CONTROL,
        correlation_id="correlation-7",
        timestamp=NOW,
        payload=payload.to_mapping(),
        visibility=visibility,
        observed_state_version=4,
        causation_event_id="event-7",
    )


def make_build_outcome(*, rejected: bool = False) -> BuildPlanReady | BuildReject:
    claim = make_claim()
    if rejected:
        return BuildReject(
            plan=ControlRejectPlan(
                game_id="game-1",
                session_id="session-1",
                group_id="group-1",
                command_id="command-7",
                command_type=SessionCommandType.PAUSE_GAME,
                operation_id="operation-7",
                operation_claim_id=claim.claim_id,
                input_event_id="event-7",
                input_sequence_no=7,
                expected_state_version=4,
                expected_cursor=6,
                expected_binding_version=2,
                rejection_event=make_result_event(rejected=True),
                operation_terminal_state=ControlOperationStatus.FAILED,
            )
        )
    return BuildPlanReady(
        plan=ControlApplyPlan(
            game_id="game-1",
            session_id="session-1",
            group_id="group-1",
            command_id="command-7",
            command_type=SessionCommandType.PAUSE_GAME,
            operation_id="operation-7",
            operation_claim_id=claim.claim_id,
            input_event_id="event-7",
            input_sequence_no=7,
            expected_state_version=4,
            expected_cursor=6,
            expected_binding_version=2,
            candidate_snapshot=CandidateSessionSnapshot(
                game_id="game-1",
                session_id="session-1",
                group_id="group-1",
                dm_participant_id="participant-dm",
                status=GameSessionStatus.PAUSED,
                current_phase=GamePhase.EXPLORATION,
                state_version=5,
                last_applied_sequence_no=7,
            ),
            participant_mutations=(),
            setup_mutations=(),
            ownership_intent=OwnershipIntent(
                intent_type=OwnershipIntentType.RETAIN,
                expected_generation=3,
                resulting_generation=3,
            ),
            result_events=(make_result_event(),),
            operation_terminal_state=ControlOperationStatus.SUCCESS,
        )
    )


def make_receipt(
    outcome: BuildPlanReady | BuildReject,
) -> ControlApplyReceipt:
    claim = make_claim()
    if isinstance(outcome, BuildReject):
        event = outcome.plan.rejection_event
        committed_version = 4
        status = ControlOperationStatus.FAILED
    else:
        event = outcome.plan.result_events[0]
        committed_version = 5
        status = ControlOperationStatus.SUCCESS
    return ControlApplyReceipt(
        game_id="game-1",
        session_id="session-1",
        committed_state_version=committed_version,
        committed_cursor=7,
        result_event_ids=(event.event_id,),
        operation_status=status,
        ownership_generation=3,
        commit_evidence_reference="commit-evidence-1",
        command_id="command-7",
        operation_id="operation-7",
        operation_claim_id=claim.claim_id,
        input_event_id="event-7",
        input_sequence_no=7,
        result_event_references=(
            CommittedResultEventReference(
                event_id=event.event_id,
                sequence_no=8,
                event_type=event.event_type,
                stored_event_reference=event.event_id,
            ),
        ),
    )


def make_handoff(*, rejected: bool = False) -> CoordinatorCommitReturned:
    outcome = make_build_outcome(rejected=rejected)
    return CoordinatorCommitReturned(
        claim=make_claim(),
        build_outcome=outcome,
        receipt=make_receipt(outcome),
    )


class FixedCoordinator:
    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.calls: list[ActorValidatedControlTurnEvidence] = []

    async def coordinate(self, evidence: ActorValidatedControlTurnEvidence) -> object:
        self.calls.append(evidence)
        return self.outcome


class RecordingEvidenceFactory:
    def __init__(self, evidence: ActorValidatedControlTurnEvidence) -> None:
        self.evidence = evidence
        self.calls: list[tuple[GameSession, ControlEventDeliveryEnvelope]] = []

    def build(
        self,
        *,
        session: GameSession,
        envelope: ControlEventDeliveryEnvelope,
    ) -> ActorValidatedControlTurnEvidence:
        self.calls.append((session, envelope))
        return self.evidence


class RecordingProcessor:
    def __init__(
        self,
        result: ControlTurnCommitReady | BaseException,
        trace: list[str] | None = None,
    ) -> None:
        self.result = result
        self.trace = trace if trace is not None else []
        self.calls: list[ActorValidatedControlTurnEvidence] = []

    async def process(
        self,
        evidence: ActorValidatedControlTurnEvidence,
    ) -> ControlTurnCommitReady:
        self.trace.append("processor")
        self.calls.append(evidence)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class RecordingCommitBoundary:
    def __init__(self, trace: list[str] | None = None) -> None:
        self.trace = trace if trace is not None else []
        self.calls: list[ControlTurnCommitReady] = []
        self.delegate = ActorOwnedLifecycleSnapshotCommitBoundary(
            current_snapshot=CandidateSessionSnapshot(
                game_id="game-1",
                session_id="session-1",
                group_id="group-1",
                dm_participant_id="participant-dm",
                status=GameSessionStatus.RUNNING,
                current_phase=GamePhase.EXPLORATION,
                state_version=4,
                last_applied_sequence_no=6,
            ),
            ownership_generation=3,
        )

    def accept(
        self,
        commit_ready: ControlTurnCommitReady,
    ) -> SnapshotVisibilityAccepted:
        self.trace.append("commit_boundary")
        self.calls.append(commit_ready)
        return self.delegate.accept(commit_ready)


class RecordingRejectCompletionBoundary:
    def __init__(self, trace: list[str] | None = None) -> None:
        self.trace = trace if trace is not None else []
        self.calls: list[ControlTurnCommitReady] = []
        self.delegate = ActorOwnedControlRejectCompletionBoundary(
            game_id="game-1",
            session_id="session-1",
            current_state_version=4,
            current_cursor=6,
        )

    def accept(
        self,
        commit_ready: ControlTurnCommitReady,
    ) -> CommittedControlRejectAccepted:
        self.trace.append("reject_boundary")
        self.calls.append(commit_ready)
        return self.delegate.accept(commit_ready)


class BlockingProcessor:
    def __init__(self, result: ControlTurnCommitReady) -> None:
        self.result = result
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def process(
        self,
        evidence: ActorValidatedControlTurnEvidence,
    ) -> ControlTurnCommitReady:
        self.entered.set()
        await self.release.wait()
        return self.result


def make_session() -> GameSession:
    return GameSession(
        game_id="game-1",
        session_id="session-1",
        group_id="group-1",
        dm_identity=DMIdentity(participant_id="participant-dm", qq_id="10001"),
    )


def make_commit_ready(*, rejected: bool = False) -> ControlTurnCommitReady:
    coordinator = FixedCoordinator(make_handoff(rejected=rejected))
    return asyncio.run(ControlTurnProcessor(coordinator).process(make_evidence()))


def test_control_turn_contracts_are_immutable_and_runtime_structural() -> None:
    ready = make_commit_ready()

    assert isinstance(RecordingEvidenceFactory(make_evidence()), ActorControlTurnEvidenceFactory)
    assert isinstance(RecordingProcessor(ready), ActorControlTurnProcessor)
    assert isinstance(RecordingCommitBoundary(), ActorControlCommitBoundary)
    assert isinstance(
        RecordingRejectCompletionBoundary(),
        ActorControlRejectCompletionBoundary,
    )
    assert {field.name for field in fields(ControlTurnCommitReady)} == {
        "build_outcome",
        "accepted_receipt",
    }
    with pytest.raises(FrozenInstanceError):
        ready.accepted_receipt = ready.accepted_receipt  # type: ignore[misc]


def test_commit_ready_rejects_an_outcome_from_another_commit() -> None:
    accepted = make_commit_ready()

    with pytest.raises(ValueError):
        ControlTurnCommitReady(
            build_outcome=make_build_outcome(rejected=True),
            accepted_receipt=accepted.accepted_receipt,
        )


def test_processor_returns_commit_ready_after_receipt_validation() -> None:
    handoff = make_handoff()
    coordinator = FixedCoordinator(handoff)

    result = asyncio.run(ControlTurnProcessor(coordinator).process(make_evidence()))

    assert result.build_outcome is handoff.build_outcome
    assert isinstance(result.accepted_receipt, ReceiptAccepted)
    assert result.accepted_receipt.commit_evidence_reference == "commit-evidence-1"
    assert len(coordinator.calls) == 1


def test_processor_accepts_atomically_committed_build_reject() -> None:
    handoff = make_handoff(rejected=True)

    result = asyncio.run(
        ControlTurnProcessor(FixedCoordinator(handoff)).process(make_evidence())
    )

    assert isinstance(result.build_outcome, BuildReject)
    assert result.accepted_receipt.committed_state_version == 4


def test_processor_rejects_invalid_receipt_fail_closed() -> None:
    handoff = make_handoff()
    invalid = replace(handoff, receipt=replace(handoff.receipt, committed_cursor=8))

    with pytest.raises(ControlTurnProcessingError) as captured:
        asyncio.run(
            ControlTurnProcessor(FixedCoordinator(invalid)).process(make_evidence())
        )

    assert captured.value.reason is ControlTurnFailureReason.RECEIPT_INVALID


def test_processor_maps_claim_conflict_without_retry() -> None:
    coordinator = FixedCoordinator(
        CoordinatorClaimConflict(
            reason=ControlApplyConflictReason.OPERATION_CLAIM_MISMATCH
        )
    )

    with pytest.raises(ControlTurnProcessingError) as captured:
        asyncio.run(ControlTurnProcessor(coordinator).process(make_evidence()))

    assert captured.value.reason is ControlTurnFailureReason.CLAIM_CONFLICT
    assert len(coordinator.calls) == 1


def test_processor_maps_apply_unknown_without_retry() -> None:
    coordinator = FixedCoordinator(
        CoordinatorApplyUnknown(
            claim=make_claim(),
            build_outcome=make_build_outcome(),
            reason=CoordinatorApplyUnknownReason.STORAGE_FAILURE,
        )
    )

    with pytest.raises(ControlTurnProcessingError) as captured:
        asyncio.run(ControlTurnProcessor(coordinator).process(make_evidence()))

    assert captured.value.reason is ControlTurnFailureReason.APPLY_UNKNOWN
    assert len(coordinator.calls) == 1


def test_actor_async_handler_calls_factory_processor_and_commit_boundary() -> None:
    evidence = make_evidence()
    trace: list[str] = []
    factory = RecordingEvidenceFactory(evidence)
    processor = RecordingProcessor(make_commit_ready(), trace)
    boundary = RecordingCommitBoundary(trace)
    reject_boundary = RecordingRejectCompletionBoundary(trace)
    actor = GameSessionActor(
        make_session(),
        control_turn_evidence_factory=factory,
        control_turn_processor=processor,
        control_commit_boundary=boundary,
        control_reject_completion_boundary=reject_boundary,
    )

    assert inspect.iscoroutinefunction(actor.handle_control_turn)
    assert asyncio.run(actor.handle_control_turn(evidence.envelope)) is None

    assert factory.calls == [(actor.session, evidence.envelope)]
    assert processor.calls == [evidence]
    assert boundary.calls == [processor.result]
    assert boundary.delegate.current_snapshot.status is GameSessionStatus.PAUSED
    assert trace == ["processor", "commit_boundary"]


def test_actor_handler_propagates_failure_without_commit_boundary() -> None:
    evidence = make_evidence()
    failure = ControlTurnProcessingError(ControlTurnFailureReason.APPLY_UNKNOWN)
    boundary = RecordingCommitBoundary()
    reject_boundary = RecordingRejectCompletionBoundary()
    actor = GameSessionActor(
        make_session(),
        control_turn_evidence_factory=RecordingEvidenceFactory(evidence),
        control_turn_processor=RecordingProcessor(failure),
        control_commit_boundary=boundary,
        control_reject_completion_boundary=reject_boundary,
    )

    with pytest.raises(ControlTurnProcessingError) as captured:
        asyncio.run(actor.handle_control_turn(evidence.envelope))

    assert captured.value is failure
    assert boundary.calls == []


def test_actor_rejects_commit_ready_claim_from_another_turn() -> None:
    evidence = make_evidence()
    ready = make_commit_ready()
    assert isinstance(ready.build_outcome, BuildPlanReady)
    forged_claim_id = "control-claim-v1:" + "0" * 64
    forged_ready = ControlTurnCommitReady(
        build_outcome=BuildPlanReady(
            plan=replace(
                ready.build_outcome.plan,
                operation_claim_id=forged_claim_id,
            )
        ),
        accepted_receipt=replace(
            ready.accepted_receipt,
            claim_id=forged_claim_id,
        ),
    )
    boundary = RecordingCommitBoundary()
    reject_boundary = RecordingRejectCompletionBoundary()
    actor = GameSessionActor(
        make_session(),
        control_turn_evidence_factory=RecordingEvidenceFactory(evidence),
        control_turn_processor=RecordingProcessor(forged_ready),
        control_commit_boundary=boundary,
        control_reject_completion_boundary=reject_boundary,
    )

    with pytest.raises(ActorControlTurnError):
        asyncio.run(actor.handle_control_turn(evidence.envelope))

    assert boundary.calls == []


def test_actor_fails_closed_when_handler_is_called_concurrently_without_gate() -> None:
    ready = make_commit_ready()

    async def scenario() -> None:
        evidence = make_evidence()
        processor = BlockingProcessor(ready)
        boundary = RecordingCommitBoundary()
        reject_boundary = RecordingRejectCompletionBoundary()
        actor = GameSessionActor(
            make_session(),
            control_turn_evidence_factory=RecordingEvidenceFactory(evidence),
            control_turn_processor=processor,
            control_commit_boundary=boundary,
            control_reject_completion_boundary=reject_boundary,
        )

        active = asyncio.create_task(actor.handle_control_turn(evidence.envelope))
        await processor.entered.wait()
        try:
            with pytest.raises(ActorControlTurnError):
                await asyncio.wait_for(
                    actor.handle_control_turn(evidence.envelope),
                    timeout=0.05,
                )
        finally:
            processor.release.set()
            await active
        assert len(boundary.calls) == 1

    asyncio.run(scenario())


def test_actor_requires_complete_control_dependencies_and_no_state_updater() -> None:
    evidence = make_evidence()
    factory = RecordingEvidenceFactory(evidence)
    processor = RecordingProcessor(make_commit_ready())
    boundary = RecordingCommitBoundary()
    reject_boundary = RecordingRejectCompletionBoundary()

    with pytest.raises(TypeError):
        GameSessionActor(
            make_session(),
            control_turn_evidence_factory=factory,
        )
    with pytest.raises(ValueError):
        GameSessionActor(
            make_session(),
            state_updater=lambda _session, _event: None,
            control_turn_evidence_factory=factory,
            control_turn_processor=processor,
            control_commit_boundary=boundary,
            control_reject_completion_boundary=reject_boundary,
        )


def test_gate_can_consume_actor_handler_and_fault_stops_on_failure() -> None:
    async def scenario() -> None:
        evidence = make_evidence()
        actor = GameSessionActor(
            make_session(),
            control_turn_evidence_factory=RecordingEvidenceFactory(evidence),
            control_turn_processor=RecordingProcessor(
                ControlTurnProcessingError(ControlTurnFailureReason.APPLY_UNKNOWN)
            ),
            control_commit_boundary=RecordingCommitBoundary(),
            control_reject_completion_boundary=RecordingRejectCompletionBoundary(),
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

        assert gate.lifecycle is GateLifecycle.FAULTED
        assert isinstance(gate.fault, ControlTurnProcessingError)
        with pytest.raises(GateNotAcceptingError):
            await gate.admit(evidence.envelope)
        assert isinstance(actor._mailbox, deque)
        assert not any(isinstance(value, asyncio.Queue) for value in vars(actor).values())

    asyncio.run(scenario())


def test_integration_sources_have_no_forbidden_dependency_or_second_queue() -> None:
    root = Path(__file__).parents[2]
    paths = (
        root / "game_runtime" / "actor" / "session_actor.py",
        root / "game_runtime" / "session_control" / "control_turn_contract.py",
        root / "game_runtime" / "session_control" / "control_turn_processor.py",
    )
    forbidden_modules = (
        "persistence",
        "recovery",
        "lifecycle_phase_builder",
        "setup_participant_builder",
        "plugin",
        "llm",
        "memory",
    )
    forbidden_calls = {
        "claim_operation",
        "commit_control_apply",
        "commit_control_rejection",
        "append_event",
        "create_task",
        "send",
    }

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
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
            forbidden in module
            for module in imports
            for forbidden in forbidden_modules
        )
        assert not calls.intersection(forbidden_calls)
        assert "Queue" not in calls
