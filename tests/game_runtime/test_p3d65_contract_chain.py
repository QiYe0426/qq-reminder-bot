from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from typing import Callable

import pytest

from game_runtime.actor.async_gate import (
    GateAdmission,
    GateLifecycle,
    GateNotAcceptingError,
    GateScopeError,
    SequenceGapError,
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
    CoordinatorClaimConflict,
    CoordinatorClaimUnknown,
    CoordinatorNonCommit,
)
from game_runtime.session_control.apply_plan_builder import (
    BuildNonCommit,
    BuildNonCommitReason,
    BuildOutcome,
    BuildPlanReady,
    BuildReject,
)
from game_runtime.session_control.build_context import (
    CanonicalControlCommandIntent,
    ControlApplyBuildContext,
    ControlApplyBuildContextError,
    ControlOwnershipBuildEvidence,
    ControlParticipantBuildView,
    ControlSessionBuildView,
    ControlSetupBuildView,
)
from game_runtime.session_control.claim_bridge import (
    OperationClaimBridge,
    OperationClaimConflict,
    OperationClaimUnknown,
    OperationClaimUnknownReason,
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
from game_runtime.session_control.lifecycle_evidence import ControlLifecycleApplyEvidence
from game_runtime.session_control.operation import ControlOperationStatus
from game_runtime.session_control.receipt_validation import (
    ReceiptAccepted,
    ReceiptInvalid,
    ReceiptInvalidReason,
    validate_control_receipt,
)
from game_runtime.session_control.result_notification import (
    CommittedResultEventNotification,
    ResultEventNotificationContractError,
    create_committed_result_event_notification,
)
from game_runtime.session_control.setup_participant_evidence import (
    ControlSetupParticipantApplyEvidence,
)


NOW = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)


def make_envelope(
    sequence_no: int = 7,
    *,
    event_id: str | None = None,
    game_id: str = "game-1",
    session_id: str = "session-1",
) -> ControlEventDeliveryEnvelope:
    suffix = str(sequence_no)
    event_id = event_id or f"event-{suffix}"
    command_id = f"command-{suffix}"
    operation_id = f"operation-{suffix}"
    observed_state_version = 4 if sequence_no == 7 else sequence_no - 1
    payload = PauseGamePayload(reason_code="DM_REQUEST")
    payload_fingerprint = fingerprint_payload(payload)
    event = GameEvent(
        event_id=event_id,
        game_id=game_id,
        session_id=session_id,
        event_type=GameEventType.DM_COMMAND,
        actor="participant-dm",
        source=GameEventSource.CONTROL,
        correlation_id=f"correlation-{suffix}",
        timestamp=NOW,
        payload={
            "command_id": command_id,
            "command_type": SessionCommandType.PAUSE_GAME.value,
            "requester": "participant-dm",
            "causation_event_id": f"request-event-{suffix}",
            "observed_state_version": observed_state_version,
            "payload_reference": f"sha256:{payload_fingerprint}",
            "payload_fingerprint": payload_fingerprint,
            "visibility": EventVisibility.DM_CONTROL.value,
        },
        visibility=EventVisibility.DM_CONTROL,
        observed_state_version=observed_state_version,
        causation_event_id=f"request-event-{suffix}",
    )
    return ControlEventDeliveryEnvelope(
        event=event,
        event_sequence_no=sequence_no,
        operation_id=operation_id,
        command_id=command_id,
        observed_state_version=observed_state_version,
        requester_principal_ref="participant-dm",
        requester_binding_version=2,
        authorization_reference=f"authorization-{suffix}",
        confirmation_reference=f"confirmation-{suffix}",
        correlation_id=f"correlation-{suffix}",
        stored_event_reference=event_id,
    )


def make_evidence(
    envelope: ControlEventDeliveryEnvelope | None = None,
) -> ActorValidatedControlTurnEvidence:
    envelope = envelope or make_envelope()
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


def claim_for(evidence: ActorValidatedControlTurnEvidence) -> ControlOperationClaim:
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


def make_build_outcome(
    claim: ControlOperationClaim,
    kind: str = "ready",
) -> BuildOutcome:
    if kind == "noncommit":
        return BuildNonCommit(
            reason=BuildNonCommitReason.RECOVERY_REQUIRED,
            detail_code="RECOVERY_REQUIRED",
        )
    if kind == "reject":
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
    if kind != "ready":
        raise ValueError("unknown outcome kind")
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
    claim: ControlOperationClaim,
    outcome: BuildPlanReady | BuildReject,
) -> ControlApplyReceipt:
    if isinstance(outcome, BuildPlanReady):
        event = outcome.plan.result_events[0]
        state_version = outcome.plan.candidate_snapshot.state_version
        status = ControlOperationStatus.SUCCESS
    else:
        event = outcome.plan.rejection_event
        state_version = outcome.plan.expected_state_version
        status = ControlOperationStatus.FAILED
    return ControlApplyReceipt(
        game_id="game-1",
        session_id="session-1",
        committed_state_version=state_version,
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


class RecordingApplyPort:
    def __init__(
        self,
        *,
        claim: ControlOperationClaim | None = None,
        receipt: ControlApplyReceipt | None = None,
        claim_error: BaseException | None = None,
        trace: list[str] | None = None,
    ) -> None:
        self.claim = claim
        self.receipt = receipt
        self.claim_error = claim_error
        self.trace = trace if trace is not None else []
        self.claim_calls = 0

    async def claim_operation(self, **values: object) -> ControlOperationClaim:
        self.trace.append("claim")
        self.claim_calls += 1
        if self.claim_error is not None:
            raise self.claim_error
        return self.claim or ControlOperationClaim(**values)  # type: ignore[arg-type]

    async def commit_control_apply(
        self,
        plan: ControlApplyPlan,
        claim: ControlOperationClaim,
    ) -> ControlApplyReceipt:
        self.trace.append("commit_apply")
        assert self.receipt is not None
        return self.receipt

    async def commit_control_rejection(
        self,
        plan: ControlRejectPlan,
        claim: ControlOperationClaim,
    ) -> ControlApplyReceipt:
        self.trace.append("commit_reject")
        assert self.receipt is not None
        return self.receipt


class RecordingBuilder:
    def __init__(self, outcome: BuildOutcome, trace: list[str] | None = None) -> None:
        self.outcome = outcome
        self.trace = trace if trace is not None else []
        self.contexts: list[ControlApplyBuildContext] = []

    def build(self, context: ControlApplyBuildContext) -> BuildOutcome:
        self.trace.append("build")
        self.contexts.append(context)
        return self.outcome


def coordinate(kind: str = "ready") -> tuple[
    object,
    list[str],
    RecordingApplyPort,
    RecordingBuilder,
    ActorValidatedControlTurnEvidence,
]:
    evidence = make_evidence()
    claim = claim_for(evidence)
    outcome = make_build_outcome(claim, kind)
    receipt = (
        make_receipt(claim, outcome)
        if isinstance(outcome, (BuildPlanReady, BuildReject))
        else None
    )
    trace: list[str] = []
    port = RecordingApplyPort(claim=claim, receipt=receipt, trace=trace)
    builder = RecordingBuilder(outcome, trace)
    coordinator = ActorOwnedApplyCoordinator(apply_port=port, builder=builder)
    result = asyncio.run(coordinator.coordinate(evidence))
    return result, trace, port, builder, evidence


def accepted_handoff() -> tuple[
    CoordinatorCommitReturned,
    ReceiptAccepted,
    GameEvent,
]:
    result, _, _, _, _ = coordinate()
    assert isinstance(result, CoordinatorCommitReturned)
    validation = validate_control_receipt(result)
    assert isinstance(validation, ReceiptAccepted)
    assert isinstance(result.build_outcome, BuildPlanReady)
    return result, validation, result.build_outcome.plan.result_events[0]


def test_delivery_contract_is_immutable_and_sequence_bound() -> None:
    envelope = make_envelope()

    assert envelope.event_sequence_no == 7
    assert envelope.stored_event_reference == envelope.event.event_id
    with pytest.raises(FrozenInstanceError):
        envelope.event_sequence_no = 8  # type: ignore[misc]
    with pytest.raises(ControlEventDeliveryEnvelopeError):
        replace(envelope, event_sequence_no=0)


def test_delivery_contract_rejects_scope_mismatch() -> None:
    class NoopConsumer:
        async def handle_control_turn(
            self,
            envelope: ControlEventDeliveryEnvelope,
        ) -> None:
            raise AssertionError("scope mismatch must fail before delivery")

    async def scenario() -> None:
        gate = SessionAsyncGate(
            game_id="game-1",
            session_id="session-1",
            next_expected_sequence_no=7,
            consumer=NoopConsumer(),
            max_queue_size=1,
        )
        await gate.start()
        with pytest.raises(GateScopeError):
            await gate.admit(
                make_envelope(game_id="game-other", session_id="session-other")
            )
        await gate.close()

    asyncio.run(scenario())


def test_gate_contract_enforces_fifo_duplicate_gap_and_fault_stop() -> None:
    class Consumer:
        def __init__(self, *, fail: bool = False) -> None:
            self.fail = fail
            self.sequences: list[int] = []

        async def handle_control_turn(
            self,
            envelope: ControlEventDeliveryEnvelope,
        ) -> None:
            if self.fail:
                raise RuntimeError("consumer failed")
            self.sequences.append(envelope.event_sequence_no)

    async def scenario() -> None:
        consumer = Consumer()
        gate = SessionAsyncGate(
            game_id="game-1",
            session_id="session-1",
            next_expected_sequence_no=1,
            consumer=consumer,
            max_queue_size=4,
        )
        await gate.start()
        first = make_envelope(1)
        assert await gate.admit(first) is GateAdmission.ACCEPTED
        assert await gate.admit(first) is GateAdmission.DUPLICATE
        with pytest.raises(SequenceGapError):
            await gate.admit(make_envelope(3))
        assert await gate.admit(make_envelope(2)) is GateAdmission.ACCEPTED
        await gate.close()
        assert consumer.sequences == [1, 2]

        failing_gate = SessionAsyncGate(
            game_id="game-1",
            session_id="session-1",
            next_expected_sequence_no=1,
            consumer=Consumer(fail=True),
            max_queue_size=1,
        )
        await failing_gate.start()
        await failing_gate.admit(first)
        await failing_gate.wait_faulted()
        assert failing_gate.lifecycle is GateLifecycle.FAULTED
        with pytest.raises(GateNotAcceptingError):
            await failing_gate.admit(make_envelope(2))

    asyncio.run(scenario())


def test_claim_contract_covers_created_executing_duplicate_conflict_and_unknown() -> None:
    envelope = make_envelope()
    expected = claim_for(make_evidence(envelope))

    class StatefulClaimPort(RecordingApplyPort):
        def __init__(self) -> None:
            super().__init__()
            self.status = ControlOperationStatus.CREATED
            self.owner: ControlOperationClaim | None = None

        async def claim_operation(self, **values: object) -> ControlOperationClaim:
            self.claim_calls += 1
            candidate = ControlOperationClaim(**values)  # type: ignore[arg-type]
            if self.owner is None:
                self.status = ControlOperationStatus.EXECUTING
                self.owner = candidate
                return candidate
            if candidate == self.owner:
                return self.owner
            raise ControlApplyConflict(
                ControlApplyConflictReason.OPERATION_CLAIM_MISMATCH,
                expected=self.owner.claim_id,
                actual=candidate.claim_id,
            )

    port = StatefulClaimPort()
    bridge = OperationClaimBridge(port)
    first = asyncio.run(bridge.acquire(envelope, expected.claim_id, NOW))
    duplicate = asyncio.run(bridge.acquire(envelope, expected.claim_id, NOW))

    assert port.status is ControlOperationStatus.EXECUTING
    assert first == duplicate == expected
    with pytest.raises(OperationClaimConflict):
        asyncio.run(
            bridge.acquire(
                envelope,
                "different-claim",
                NOW + timedelta(seconds=1),
            )
        )

    unknown = OperationClaimBridge(
        RecordingApplyPort(
            claim_error=ControlApplyStorageFailure("storage unavailable")
        )
    )
    with pytest.raises(OperationClaimUnknown) as captured:
        asyncio.run(unknown.acquire(envelope, expected.claim_id, NOW))
    assert captured.value.reason is OperationClaimUnknownReason.STORAGE_FAILURE


def test_builder_contract_is_immutable_evidence_bound_and_deterministic() -> None:
    _, _, _, builder, _ = coordinate()
    context = builder.contexts[0]

    assert isinstance(context.participant_views, tuple)
    with pytest.raises(FrozenInstanceError):
        context.session_view = replace(context.session_view, state_version=5)  # type: ignore[misc]
    with pytest.raises(ControlApplyBuildContextError):
        replace(
            context,
            ownership_evidence=replace(
                context.ownership_evidence,
                observed_state_version=5,
            ),
        )
    assert builder.build(context) == builder.build(context)
    assert (
        context.result_event_seed.derive_event_id(
            GameEventType.SESSION_PAUSED,
            ordinal=1,
        )
        == context.result_event_seed.derive_event_id(
            GameEventType.SESSION_PAUSED,
            ordinal=1,
        )
    )


@pytest.mark.parametrize(
    ("kind", "expected_type", "expected_trace"),
    (
        ("ready", CoordinatorCommitReturned, ["claim", "build", "commit_apply"]),
        ("reject", CoordinatorCommitReturned, ["claim", "build", "commit_reject"]),
        ("noncommit", CoordinatorNonCommit, ["claim", "build"]),
    ),
)
def test_coordinator_contract_orders_claim_build_commit_without_state_mutation(
    kind: str,
    expected_type: type[object],
    expected_trace: list[str],
) -> None:
    result, trace, _, builder, evidence = coordinate(kind)

    assert isinstance(result, expected_type)
    assert trace == expected_trace
    assert evidence.session_view == make_evidence().session_view
    assert builder.contexts[0].session_view is evidence.session_view


@pytest.mark.parametrize(
    ("claim_error", "expected_type"),
    (
        (
            ControlApplyConflict(
                ControlApplyConflictReason.OPERATION_CLAIM_MISMATCH
            ),
            CoordinatorClaimConflict,
        ),
        (ControlApplyStorageFailure("unknown"), CoordinatorClaimUnknown),
    ),
)
def test_coordinator_claim_failure_stops_before_build_and_commit(
    claim_error: BaseException,
    expected_type: type[object],
) -> None:
    evidence = make_evidence()
    trace: list[str] = []
    port = RecordingApplyPort(claim_error=claim_error, trace=trace)
    builder = RecordingBuilder(make_build_outcome(claim_for(evidence)), trace)
    coordinator = ActorOwnedApplyCoordinator(apply_port=port, builder=builder)

    result = asyncio.run(coordinator.coordinate(evidence))

    assert isinstance(result, expected_type)
    assert trace == ["claim"]
    assert builder.contexts == []


def test_receipt_contract_returns_immutable_accepted_proof() -> None:
    _, accepted, _ = accepted_handoff()

    assert accepted.committed_cursor == accepted.input_sequence_no == 7
    assert accepted.operation_id == "operation-7"
    assert accepted.result_event_references[0].event_id == "event-session-paused"
    with pytest.raises(FrozenInstanceError):
        accepted.committed_cursor = 8  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    (
        ("committed_cursor", 8, ReceiptInvalidReason.CURSOR_MISMATCH),
        ("committed_state_version", 6, ReceiptInvalidReason.VERSION_MISMATCH),
        ("operation_id", "operation-other", ReceiptInvalidReason.OPERATION_MISMATCH),
        (
            "ownership_generation",
            4,
            ReceiptInvalidReason.OWNERSHIP_MISMATCH,
        ),
    ),
)
def test_receipt_contract_rejects_invalid_commit_evidence(
    field: str,
    value: object,
    reason: ReceiptInvalidReason,
) -> None:
    handoff, _, _ = accepted_handoff()
    invalid_handoff = replace(
        handoff,
        receipt=replace(handoff.receipt, **{field: value}),
    )

    result = validate_control_receipt(invalid_handoff)

    assert result == ReceiptInvalid(reason)


def test_notification_contract_derives_only_from_accepted_receipt() -> None:
    _, accepted, event = accepted_handoff()
    reference = accepted.result_event_references[0]

    notification = create_committed_result_event_notification(
        accepted_receipt=accepted,
        event=event,
        event_reference=reference,
        commit_evidence_reference=accepted.commit_evidence_reference,
    )

    assert isinstance(notification, CommittedResultEventNotification)
    assert notification.event is event
    assert notification.operation_reference == accepted.operation_id
    assert notification.commit_evidence_reference == accepted.commit_evidence_reference


@pytest.mark.parametrize(
    "mutation",
    (
        lambda accepted, event, reference: {
            "accepted_receipt": accepted,
            "event": replace(event, event_id="event-other"),
            "event_reference": reference,
            "commit_evidence_reference": accepted.commit_evidence_reference,
        },
        lambda accepted, event, reference: {
            "accepted_receipt": accepted,
            "event": replace(
                event,
                payload={**event.payload, "operation_id": "operation-other"},
            ),
            "event_reference": reference,
            "commit_evidence_reference": accepted.commit_evidence_reference,
        },
        lambda accepted, event, reference: {
            "accepted_receipt": accepted,
            "event": event,
            "event_reference": reference,
            "commit_evidence_reference": "commit-evidence-other",
        },
    ),
)
def test_notification_contract_rejects_event_operation_or_commit_mismatch(
    mutation: Callable[
        [ReceiptAccepted, GameEvent, CommittedResultEventReference],
        dict[str, object],
    ],
) -> None:
    _, accepted, event = accepted_handoff()
    values = mutation(accepted, event, accepted.result_event_references[0])

    with pytest.raises(ResultEventNotificationContractError):
        create_committed_result_event_notification(**values)  # type: ignore[arg-type]


def test_complete_actor_apply_adapter_contract_chain() -> None:
    evidence = make_evidence()
    claim = claim_for(evidence)
    outcome = make_build_outcome(claim)
    assert isinstance(outcome, BuildPlanReady)
    trace: list[str] = []
    port = RecordingApplyPort(
        claim=claim,
        receipt=make_receipt(claim, outcome),
        trace=trace,
    )
    builder = RecordingBuilder(outcome, trace)
    coordinator = ActorOwnedApplyCoordinator(apply_port=port, builder=builder)
    delivered: list[CommittedResultEventNotification] = []

    class ChainConsumer:
        async def handle_control_turn(
            self,
            envelope: ControlEventDeliveryEnvelope,
        ) -> None:
            trace.append("gate")
            assert envelope == evidence.envelope
            handoff = await coordinator.coordinate(evidence)
            assert isinstance(handoff, CoordinatorCommitReturned)
            accepted = validate_control_receipt(handoff)
            assert isinstance(accepted, ReceiptAccepted)
            trace.append("receipt")
            delivered.append(
                create_committed_result_event_notification(
                    accepted_receipt=accepted,
                    event=outcome.plan.result_events[0],
                    event_reference=accepted.result_event_references[0],
                    commit_evidence_reference=accepted.commit_evidence_reference,
                )
            )
            trace.append("notification")

    async def scenario() -> None:
        gate = SessionAsyncGate(
            game_id="game-1",
            session_id="session-1",
            next_expected_sequence_no=7,
            consumer=ChainConsumer(),
            max_queue_size=1,
        )
        await gate.start()
        assert await gate.admit(evidence.envelope) is GateAdmission.ACCEPTED
        await gate.close()

    asyncio.run(scenario())

    assert trace == [
        "gate",
        "claim",
        "build",
        "commit_apply",
        "receipt",
        "notification",
    ]
    assert delivered[0].event is outcome.plan.result_events[0]
    assert delivered[0].operation_reference == claim.operation_id
    assert delivered[0].input_event_reference == claim.input_event_id
