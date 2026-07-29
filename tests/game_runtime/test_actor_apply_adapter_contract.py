from __future__ import annotations

import ast
import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import pytest

from game_runtime.actor.async_gate import (
    GateAdmission,
    GateLifecycle,
    SequenceConflictError,
    SessionAsyncGate,
)
from game_runtime.event import (
    EventVisibility,
    GameEvent,
    GameEventSource,
    GameEventType,
    SessionControlRejectedPayload,
    SessionPausedPayload,
)
from game_runtime.participant import ParticipantMembershipState, ParticipantType
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control.apply_contract import (
    CandidateSessionSnapshot,
    CommittedResultEventReference,
    ControlApplyConflict,
    ControlApplyConflictReason,
    ControlApplyPlan,
    ControlApplyReceipt,
    ControlApplyStorageFailure,
    ControlOperationClaim,
    ControlRejectPlan,
    OwnershipIntent,
    OwnershipIntentType,
)
from game_runtime.session_control.apply_coordinator import (
    ActorOwnedApplyCoordinator,
    CoordinatorNonCommit,
    CoordinatorClaimConflict,
    CoordinatorClaimUnknown,
    CoordinatorClaimUnknownReason,
    CoordinatorOutcome,
)
from game_runtime.session_control.apply_plan_builder import (
    BuildNonCommit,
    BuildNonCommitReason,
    BuildOutcome,
    BuildPlanReady,
    BuildReject,
    ControlApplyPlanBuilder,
)
from game_runtime.session_control.build_context import (
    CanonicalControlCommandIntent,
    ControlApplyBuildContext,
    ControlOwnershipBuildEvidence,
    ControlParticipantBuildView,
    ControlResultEventSeed,
    ControlSessionBuildView,
    ControlSetupBuildView,
)
from game_runtime.session_control.claim_bridge import (
    OperationClaimBridge,
    OperationClaimConflict,
    OperationClaimUnknown,
)
from game_runtime.session_control.commands import PauseGamePayload, SessionCommandType
from game_runtime.session_control.confirmation import fingerprint_payload
from game_runtime.session_control.coordinator_evidence import (
    ActorValidatedControlTurnEvidence,
    ControlClaimAttemptEvidence,
    CoordinatorCommitReturned,
)
from game_runtime.session_control.delivery import (
    ControlEventDeliveryEnvelope,
    ControlEventDeliveryEnvelopeError,
)
from game_runtime.session_control.operation import ControlOperationStatus
from game_runtime.session_control.lifecycle_evidence import ControlLifecycleApplyEvidence
from game_runtime.session_control.setup_participant_evidence import (
    ControlSetupParticipantApplyEvidence,
)
from game_runtime.session_control.receipt_validation import (
    ReceiptAccepted,
    ReceiptInvalid,
    validate_control_receipt,
)
from game_runtime.session_control.result_notification import (
    CommittedResultEventNotification,
)


NOW = datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)


class TraceStep(str, Enum):
    ADMITTED = "ADMITTED"
    VALIDATED = "VALIDATED"
    CLAIMED = "CLAIMED"
    BUILT = "BUILT"
    COMMITTED = "COMMITTED"
    RECEIPT_ACCEPTED = "RECEIPT_ACCEPTED"
    NOTIFIED = "NOTIFIED"


class ClaimMode(str, Enum):
    SUCCESS = "SUCCESS"
    CONFLICT = "CONFLICT"
    UNKNOWN = "UNKNOWN"


class ContractTurnStopped(RuntimeError):
    pass


class NotificationDeliveryFailure(RuntimeError):
    pass


def make_envelope(
    *,
    event_id: str = "event-7",
    event_type: GameEventType = GameEventType.DM_COMMAND,
    sequence_no: int = 7,
) -> ControlEventDeliveryEnvelope:
    command_payload = PauseGamePayload(reason_code="DM_REQUEST")
    payload_fingerprint = fingerprint_payload(command_payload)
    event = GameEvent(
        event_id=event_id,
        game_id="game-1",
        session_id="session-1",
        event_type=event_type,
        actor="participant-dm",
        source=GameEventSource.CONTROL,
        correlation_id="correlation-1",
        timestamp=NOW,
        payload={
            "command_id": "command-1",
            "command_type": SessionCommandType.PAUSE_GAME.value,
            "requester": "participant-dm",
            "causation_event_id": "request-event-1",
            "observed_state_version": 4,
            "payload_reference": f"sha256:{payload_fingerprint}",
            "payload_fingerprint": payload_fingerprint,
            "visibility": EventVisibility.DM_CONTROL.value,
        },
        visibility=EventVisibility.DM_CONTROL,
        observed_state_version=4,
        causation_event_id="request-event-1",
    )
    return ControlEventDeliveryEnvelope(
        event=event,
        event_sequence_no=sequence_no,
        operation_id="operation-1",
        command_id="command-1",
        observed_state_version=4,
        requester_principal_ref="participant-dm",
        requester_binding_version=2,
        authorization_reference="authorization-1",
        confirmation_reference="confirmation-1",
        correlation_id="correlation-1",
        stored_event_reference=event_id,
    )


def make_validated_evidence(
    envelope: ControlEventDeliveryEnvelope,
) -> ActorValidatedControlTurnEvidence:
    command_payload = PauseGamePayload(reason_code="DM_REQUEST")
    return ActorValidatedControlTurnEvidence(
        envelope=envelope,
        command_intent=CanonicalControlCommandIntent(
            intent_schema_version=1,
            command_type=SessionCommandType.PAUSE_GAME,
            payload=command_payload,
            payload_fingerprint=fingerprint_payload(command_payload),
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
        authorization_reference="authorization-1",
        confirmation_reference="confirmation-1",
        lifecycle_evidence=ControlLifecycleApplyEvidence(),
        setup_participant_evidence=ControlSetupParticipantApplyEvidence(),
        claim_attempt=ControlClaimAttemptEvidence.from_envelope(
            envelope,
            claimed_at=NOW,
        ),
    )


def claim_from_evidence(
    evidence: ActorValidatedControlTurnEvidence,
) -> ControlOperationClaim:
    attempt = evidence.claim_attempt
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
            command_id="command-1",
            operation_id="operation-1",
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
            command_id="command-1",
            operation_id="operation-1",
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
        correlation_id="correlation-1",
        timestamp=NOW,
        payload=payload.to_mapping(),
        visibility=visibility,
        observed_state_version=4,
        causation_event_id="event-7",
    )


def make_build_ready(claim: ControlOperationClaim) -> BuildPlanReady:
    result_event = make_result_event()
    return BuildPlanReady(
        plan=ControlApplyPlan(
            game_id="game-1",
            session_id="session-1",
            group_id="group-1",
            command_id="command-1",
            command_type=SessionCommandType.PAUSE_GAME,
            operation_id="operation-1",
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
            result_events=(result_event,),
            operation_terminal_state=ControlOperationStatus.SUCCESS,
        )
    )


def make_build_reject(claim: ControlOperationClaim) -> BuildReject:
    return BuildReject(
        plan=ControlRejectPlan(
            game_id="game-1",
            session_id="session-1",
            group_id="group-1",
            command_id="command-1",
            command_type=SessionCommandType.PAUSE_GAME,
            operation_id="operation-1",
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


def make_receipt(
    claim: ControlOperationClaim,
    outcome: BuildPlanReady | BuildReject,
) -> ControlApplyReceipt:
    if isinstance(outcome, BuildPlanReady):
        event = outcome.plan.result_events[0]
        state_version = outcome.plan.candidate_snapshot.state_version
        operation_status = ControlOperationStatus.SUCCESS
    else:
        event = outcome.plan.rejection_event
        state_version = outcome.plan.expected_state_version
        operation_status = ControlOperationStatus.FAILED
    return ControlApplyReceipt(
        game_id="game-1",
        session_id="session-1",
        committed_state_version=state_version,
        committed_cursor=7,
        result_event_ids=(event.event_id,),
        operation_status=operation_status,
        ownership_generation=3,
        commit_evidence_reference="commit-evidence-1",
        command_id="command-1",
        operation_id="operation-1",
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


class ScriptedApplyPort:
    def __init__(
        self,
        *,
        receipt: ControlApplyReceipt | None,
        claim_mode: ClaimMode,
    ) -> None:
        self.receipt = receipt
        self.claim_mode = claim_mode
        self.calls: list[tuple[str, object]] = []

    async def claim_operation(self, **values: object) -> ControlOperationClaim:
        self.calls.append(("claim_operation", dict(values)))
        if self.claim_mode is ClaimMode.CONFLICT:
            raise ControlApplyConflict(
                ControlApplyConflictReason.OPERATION_CLAIM_MISMATCH
            )
        if self.claim_mode is ClaimMode.UNKNOWN:
            raise ControlApplyStorageFailure("claim outcome unavailable")
        return ControlOperationClaim(**values)  # type: ignore[arg-type]

    async def commit_control_apply(
        self,
        plan: ControlApplyPlan,
        claim: ControlOperationClaim,
    ) -> ControlApplyReceipt:
        self.calls.append(("commit_control_apply", (plan, claim)))
        assert self.receipt is not None
        return self.receipt

    async def commit_control_rejection(
        self,
        plan: ControlRejectPlan,
        claim: ControlOperationClaim,
    ) -> ControlApplyReceipt:
        self.calls.append(("commit_control_rejection", (plan, claim)))
        assert self.receipt is not None
        return self.receipt


class ScriptedBuilder:
    def __init__(self, outcome: BuildOutcome) -> None:
        self.outcome = outcome
        self.contexts: list[ControlApplyBuildContext] = []

    def build(self, context: ControlApplyBuildContext) -> BuildOutcome:
        self.contexts.append(context)
        return self.outcome


class ContractCoordinator:
    def __init__(
        self,
        *,
        bridge: OperationClaimBridge,
        builder: ControlApplyPlanBuilder,
        port: ScriptedApplyPort,
        trace: list[TraceStep],
    ) -> None:
        self.bridge = bridge
        self.builder = builder
        self.port = port
        self.trace = trace

    async def coordinate(
        self,
        evidence: ActorValidatedControlTurnEvidence,
    ) -> CoordinatorOutcome:
        attempt = evidence.claim_attempt
        try:
            claim = await self.bridge.acquire(
                evidence.envelope,
                attempt.claim_id,
                attempt.claimed_at,
            )
        except OperationClaimConflict as exc:
            return CoordinatorClaimConflict(reason=exc.reason)
        except OperationClaimUnknown as exc:
            return CoordinatorClaimUnknown(
                reason=CoordinatorClaimUnknownReason(exc.reason.value)
            )
        self.trace.append(TraceStep.CLAIMED)

        event = evidence.envelope.event
        context = ControlApplyBuildContext(
            envelope=evidence.envelope,
            claim=claim,
            command_intent=evidence.command_intent,
            session_view=evidence.session_view,
            participant_views=evidence.participant_views,
            setup_view=evidence.setup_view,
            ownership_evidence=evidence.ownership_evidence,
            result_event_seed=ControlResultEventSeed(
                seed_contract_version=1,
                game_id=event.game_id,
                session_id=event.session_id,
                command_id=evidence.envelope.command_id,
                operation_id=evidence.envelope.operation_id,
                input_event_id=event.event_id,
                timestamp=NOW,
                correlation_id=event.correlation_id,
            ),
        )
        outcome = self.builder.build(context)
        self.trace.append(TraceStep.BUILT)
        if isinstance(outcome, BuildNonCommit):
            return CoordinatorNonCommit(claim=claim, outcome=outcome)
        if isinstance(outcome, BuildPlanReady):
            receipt = await self.port.commit_control_apply(outcome.plan, claim)
        else:
            receipt = await self.port.commit_control_rejection(outcome.plan, claim)
        self.trace.append(TraceStep.COMMITTED)
        return CoordinatorCommitReturned(
            claim=claim,
            build_outcome=outcome,
            receipt=receipt,
        )


class ScriptedNotificationSink:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.attempts = 0
        self.delivered: list[CommittedResultEventNotification] = []

    def publish(self, notification: CommittedResultEventNotification) -> None:
        self.attempts += 1
        if self.fail:
            raise NotificationDeliveryFailure("notification unavailable")
        self.delivered.append(notification)


class ContractTurnConsumer:
    def __init__(
        self,
        *,
        coordinator: ActorOwnedApplyCoordinator,
        sink: ScriptedNotificationSink,
        trace: list[TraceStep],
    ) -> None:
        self.coordinator = coordinator
        self.sink = sink
        self.trace = trace
        self.admission_recorded = asyncio.Event()
        self.completed = asyncio.Event()
        self.outcome: CoordinatorOutcome | None = None
        self.validation: ReceiptAccepted | ReceiptInvalid | None = None

    async def handle_control_turn(
        self,
        envelope: ControlEventDeliveryEnvelope,
    ) -> None:
        await self.admission_recorded.wait()
        evidence = make_validated_evidence(envelope)
        self.trace.append(TraceStep.VALIDATED)
        self.outcome = await self.coordinator.coordinate(evidence)
        if not isinstance(self.outcome, CoordinatorCommitReturned):
            raise ContractTurnStopped(type(self.outcome).__name__)

        self.validation = validate_control_receipt(self.outcome)
        if not isinstance(self.validation, ReceiptAccepted):
            raise ContractTurnStopped(self.validation.reason.value)
        self.trace.append(TraceStep.RECEIPT_ACCEPTED)

        build_outcome = self.outcome.build_outcome
        events = (
            build_outcome.plan.result_events
            if isinstance(build_outcome, BuildPlanReady)
            else (build_outcome.plan.rejection_event,)
        )
        for event, reference in zip(
            events,
            self.validation.result_event_references,
        ):
            self.sink.publish(
                CommittedResultEventNotification(
                    event=event,
                    event_reference=reference,
                    operation_reference=self.outcome.claim.operation_id,
                    input_event_reference=self.outcome.claim.input_event_id,
                    commit_evidence_reference=(
                        self.validation.commit_evidence_reference
                    ),
                )
            )
        self.trace.append(TraceStep.NOTIFIED)
        self.completed.set()


@dataclass(slots=True)
class ContractHarness:
    envelope: ControlEventDeliveryEnvelope
    evidence: ActorValidatedControlTurnEvidence
    port: ScriptedApplyPort
    builder: ScriptedBuilder
    coordinator: ContractCoordinator
    sink: ScriptedNotificationSink
    consumer: ContractTurnConsumer
    gate: SessionAsyncGate
    trace: list[TraceStep]


def make_harness(
    *,
    outcome_kind: str = "ready",
    claim_mode: ClaimMode = ClaimMode.SUCCESS,
    receipt_mismatch: bool = False,
    notification_failure: bool = False,
) -> ContractHarness:
    envelope = make_envelope()
    evidence = make_validated_evidence(envelope)
    claim = claim_from_evidence(evidence)
    if outcome_kind == "ready":
        outcome: BuildOutcome = make_build_ready(claim)
    elif outcome_kind == "reject":
        outcome = make_build_reject(claim)
    elif outcome_kind == "noncommit":
        outcome = BuildNonCommit(
            reason=BuildNonCommitReason.RECOVERY_REQUIRED,
            detail_code="RECOVERY_REQUIRED",
        )
    else:
        raise ValueError("unsupported outcome_kind")
    receipt = (
        make_receipt(claim, outcome)
        if isinstance(outcome, (BuildPlanReady, BuildReject))
        else None
    )
    if receipt_mismatch:
        assert receipt is not None
        receipt = replace(receipt, committed_cursor=8)

    trace: list[TraceStep] = []
    port = ScriptedApplyPort(receipt=receipt, claim_mode=claim_mode)
    builder = ScriptedBuilder(outcome)
    coordinator = ContractCoordinator(
        bridge=OperationClaimBridge(port),
        builder=builder,
        port=port,
        trace=trace,
    )
    sink = ScriptedNotificationSink(fail=notification_failure)
    consumer = ContractTurnConsumer(
        coordinator=coordinator,
        sink=sink,
        trace=trace,
    )
    gate = SessionAsyncGate(
        game_id="game-1",
        session_id="session-1",
        next_expected_sequence_no=7,
        consumer=consumer,
        max_queue_size=1,
    )
    return ContractHarness(
        envelope=envelope,
        evidence=evidence,
        port=port,
        builder=builder,
        coordinator=coordinator,
        sink=sink,
        consumer=consumer,
        gate=gate,
        trace=trace,
    )


async def admit_and_complete(harness: ContractHarness) -> None:
    await harness.gate.start()
    admission = await harness.gate.admit(harness.envelope)
    assert admission is GateAdmission.ACCEPTED
    harness.trace.append(TraceStep.ADMITTED)
    harness.consumer.admission_recorded.set()
    await asyncio.wait_for(harness.consumer.completed.wait(), timeout=1)
    await harness.gate.close()


async def admit_and_fault(harness: ContractHarness) -> None:
    await harness.gate.start()
    admission = await harness.gate.admit(harness.envelope)
    assert admission is GateAdmission.ACCEPTED
    harness.trace.append(TraceStep.ADMITTED)
    harness.consumer.admission_recorded.set()
    await asyncio.wait_for(harness.gate.wait_faulted(), timeout=1)


def test_success_path_closes_identity_receipt_and_notification_contracts() -> None:
    harness = make_harness()

    asyncio.run(admit_and_complete(harness))

    assert harness.trace == list(TraceStep)
    assert [name for name, _ in harness.port.calls] == [
        "claim_operation",
        "commit_control_apply",
    ]
    assert len(harness.builder.contexts) == 1
    context = harness.builder.contexts[0]
    notification = harness.sink.delivered[0]
    assert (
        context.envelope.command_id,
        context.claim.command_id,
        context.result_event_seed.command_id,
    ) == ("command-1",) * 3
    assert (
        context.envelope.operation_id,
        context.claim.operation_id,
        notification.operation_reference,
    ) == ("operation-1",) * 3
    assert context.claim.claim_id == harness.evidence.claim_attempt.claim_id
    assert (
        context.envelope.event.event_id,
        context.claim.input_event_id,
        notification.input_event_reference,
    ) == ("event-7",) * 3
    assert isinstance(harness.consumer.validation, ReceiptAccepted)
    assert notification.event_reference.event_id == notification.event.event_id
    assert notification.commit_evidence_reference == "commit-evidence-1"


def test_build_reject_is_atomically_committed_before_notification() -> None:
    harness = make_harness(outcome_kind="reject")

    asyncio.run(admit_and_complete(harness))

    assert harness.trace == list(TraceStep)
    assert [name for name, _ in harness.port.calls] == [
        "claim_operation",
        "commit_control_rejection",
    ]
    assert isinstance(harness.consumer.outcome, CoordinatorCommitReturned)
    assert isinstance(harness.consumer.outcome.build_outcome, BuildReject)
    assert harness.sink.delivered[0].event.event_type is (
        GameEventType.SESSION_CONTROL_REJECTED
    )


def test_invalid_envelope_is_rejected_before_gate_or_claim() -> None:
    with pytest.raises(ControlEventDeliveryEnvelopeError):
        make_envelope(event_type=GameEventType.MESSAGE_RECEIVED)


def test_same_sequence_with_conflicting_identity_faults_without_chain_calls() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        class HoldingConsumer:
            async def handle_control_turn(
                self,
                envelope: ControlEventDeliveryEnvelope,
            ) -> None:
                started.set()
                await release.wait()

        gate = SessionAsyncGate(
            game_id="game-1",
            session_id="session-1",
            next_expected_sequence_no=7,
            consumer=HoldingConsumer(),
            max_queue_size=1,
        )
        await gate.start()
        assert await gate.admit(make_envelope()) is GateAdmission.ACCEPTED
        await asyncio.wait_for(started.wait(), timeout=1)
        with pytest.raises(SequenceConflictError):
            await gate.admit(make_envelope(event_id="event-conflict"))
        release.set()
        await asyncio.wait_for(gate.wait_faulted(), timeout=1)
        assert gate.lifecycle is GateLifecycle.FAULTED

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("claim_mode", "expected_outcome"),
    (
        (ClaimMode.CONFLICT, CoordinatorClaimConflict),
        (ClaimMode.UNKNOWN, CoordinatorClaimUnknown),
    ),
)
def test_claim_failure_stops_before_build_commit_notification_and_retry(
    claim_mode: ClaimMode,
    expected_outcome: type[CoordinatorOutcome],
) -> None:
    harness = make_harness(claim_mode=claim_mode)

    asyncio.run(admit_and_fault(harness))

    assert isinstance(harness.consumer.outcome, expected_outcome)
    assert harness.trace == [TraceStep.ADMITTED, TraceStep.VALIDATED]
    assert [name for name, _ in harness.port.calls] == ["claim_operation"]
    assert harness.builder.contexts == []
    assert harness.sink.attempts == 0
    assert harness.gate.lifecycle is GateLifecycle.FAULTED


def test_build_noncommit_stops_without_commit_notification_or_retry() -> None:
    harness = make_harness(outcome_kind="noncommit")

    asyncio.run(admit_and_fault(harness))

    assert isinstance(harness.consumer.outcome, CoordinatorNonCommit)
    assert harness.trace == [
        TraceStep.ADMITTED,
        TraceStep.VALIDATED,
        TraceStep.CLAIMED,
        TraceStep.BUILT,
    ]
    assert [name for name, _ in harness.port.calls] == ["claim_operation"]
    assert harness.sink.attempts == 0


def test_receipt_mismatch_stops_after_commit_without_notification_or_retry() -> None:
    harness = make_harness(receipt_mismatch=True)

    asyncio.run(admit_and_fault(harness))

    assert isinstance(harness.consumer.validation, ReceiptInvalid)
    assert harness.trace == [
        TraceStep.ADMITTED,
        TraceStep.VALIDATED,
        TraceStep.CLAIMED,
        TraceStep.BUILT,
        TraceStep.COMMITTED,
    ]
    assert [name for name, _ in harness.port.calls] == [
        "claim_operation",
        "commit_control_apply",
    ]
    assert harness.sink.attempts == 0
    assert harness.gate.lifecycle is GateLifecycle.FAULTED


def test_notification_failure_does_not_reclaim_recommit_or_report_delivery() -> None:
    harness = make_harness(notification_failure=True)

    asyncio.run(admit_and_fault(harness))

    assert harness.trace == [
        TraceStep.ADMITTED,
        TraceStep.VALIDATED,
        TraceStep.CLAIMED,
        TraceStep.BUILT,
        TraceStep.COMMITTED,
        TraceStep.RECEIPT_ACCEPTED,
    ]
    assert [name for name, _ in harness.port.calls] == [
        "claim_operation",
        "commit_control_apply",
    ]
    assert harness.sink.attempts == 1
    assert harness.sink.delivered == []
    assert isinstance(harness.gate.fault, NotificationDeliveryFailure)


def test_contract_harness_has_no_forbidden_runtime_or_port_bypass() -> None:
    module_path = Path(__file__)
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported_modules = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    forbidden_modules = (
        "session_actor",
        "persistence",
        "recovery",
        "sqlite",
        "ingress",
    )
    assert not any(
        forbidden in module
        for module in imported_modules
        for forbidden in forbidden_modules
    )

    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "claim_operation" not in called_attributes
    assert not called_attributes.intersection(
        {
            "append_event",
            "bind_input_event",
            "complete",
            "update_state",
            "swap",
        }
    )
    class_names = {
        node.name.lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
    }
    assert not any("reducer" in name for name in class_names)
