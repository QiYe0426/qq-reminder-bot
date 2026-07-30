from __future__ import annotations

import ast
import asyncio
from collections import deque
from dataclasses import replace
import inspect

import pytest

from game_runtime.actor.async_gate import (
    GateAdmission,
    GateLifecycle,
    SessionAsyncGate,
)
from game_runtime.actor.session_actor import GameSessionActor
from game_runtime.event import (
    GameEventType,
    SessionControlRejectedPayload,
    validate_control_result_event,
)
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control import (
    ActorOwnedApplyCoordinator,
    ActorOwnedGameStateCompletionBoundary,
    ActorValidatedControlTurnEvidence,
    ActorVisibleGameState,
    CommittedResultEventReference,
    CompositeGameControlApplyPlanBuilder,
    ControlApplyReceipt,
    ControlClaimAttemptEvidence,
    ControlCompletionKind,
    ControlEventDeliveryEnvelope,
    ControlOperationClaim,
    ControlOperationStatus,
    ControlTurnFailureReason,
    ControlTurnProcessingError,
    ControlTurnProcessor,
    OwnershipIntentType,
    SessionCommandType,
)
from game_runtime.session_control.apply_contract import (
    ControlApplyPlan,
    ControlRejectPlan,
)
from game_runtime.session_control.event_integration import DMCommandEventPayload
from test_actor_control_turn_integration_contract import make_session
from test_composite_lifecycle_promotion_contract import _current_snapshot
from test_lifecycle_phase_apply_plan_builder import make_context


class StrictRecordingAtomicApplyPort:
    """P5-1 contract fake for recording the Atomic Apply Port boundary."""

    __slots__ = (
        "block_first_claim",
        "calls",
        "failure_mode",
        "first_claim_started",
        "ownership_generation",
        "receipt_corruption",
        "release_first_claim",
    )

    def __init__(
        self,
        *,
        ownership_generation: int | None,
        failure_mode: str | None = None,
        receipt_corruption: str | None = None,
        block_first_claim: bool = False,
    ) -> None:
        if failure_mode not in {None, "invalid_receipt", "port_failure"}:
            raise ValueError("unsupported failure mode")
        self.calls: list[tuple[str, object, object | None]] = []
        self.failure_mode = failure_mode
        self.ownership_generation = ownership_generation
        self.receipt_corruption = receipt_corruption
        self.block_first_claim = block_first_claim
        self.first_claim_started = asyncio.Event()
        self.release_first_claim = asyncio.Event()

    async def claim_operation(self, **values: object) -> ControlOperationClaim:
        expected_keys = {
            "game_id",
            "session_id",
            "command_id",
            "operation_id",
            "input_event_id",
            "claim_id",
            "claimed_at",
        }
        assert set(values) == expected_keys
        self.calls.append(("claim_operation", dict(values), None))
        if self.block_first_claim and len(self.calls) == 1:
            self.first_claim_started.set()
            await self.release_first_claim.wait()
        return ControlOperationClaim(**values)  # type: ignore[arg-type]

    async def commit_control_apply(
        self,
        plan: ControlApplyPlan,
        claim: ControlOperationClaim,
    ) -> ControlApplyReceipt:
        assert isinstance(plan, ControlApplyPlan)
        assert isinstance(claim, ControlOperationClaim)
        assert claim.claim_id == plan.operation_claim_id
        self.calls.append(("commit_control_apply", plan, claim))
        if self.failure_mode == "port_failure":
            raise RuntimeError("atomic apply unavailable")
        committed_version = plan.candidate_snapshot.state_version
        if self.failure_mode == "invalid_receipt":
            committed_version += 1
        receipt = self._receipt(
            plan=plan,
            claim=claim,
            events=plan.result_events,
            committed_version=committed_version,
            operation_status=ControlOperationStatus.SUCCESS,
            ownership_generation=plan.ownership_intent.resulting_generation,
        )
        if self.failure_mode is None:
            self.ownership_generation = plan.ownership_intent.resulting_generation
        return receipt

    async def commit_control_rejection(
        self,
        plan: ControlRejectPlan,
        claim: ControlOperationClaim,
    ) -> ControlApplyReceipt:
        assert isinstance(plan, ControlRejectPlan)
        assert isinstance(claim, ControlOperationClaim)
        assert claim.claim_id == plan.operation_claim_id
        self.calls.append(("commit_control_rejection", plan, claim))
        if self.failure_mode == "port_failure":
            raise RuntimeError("atomic rejection unavailable")
        committed_version = plan.expected_state_version
        if self.failure_mode == "invalid_receipt":
            committed_version += 1
        return self._receipt(
            plan=plan,
            claim=claim,
            events=(plan.rejection_event,),
            committed_version=committed_version,
            operation_status=plan.operation_terminal_state,
            ownership_generation=self.ownership_generation,
        )

    def _receipt(
        self,
        *,
        plan: ControlApplyPlan | ControlRejectPlan,
        claim: ControlOperationClaim,
        events: tuple[object, ...],
        committed_version: int,
        operation_status: ControlOperationStatus,
        ownership_generation: int | None,
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
        result_event_ids = tuple(event.event_id for event in events)
        if self.receipt_corruption == "event_id":
            reference = references[0]
            corrupted_id = f"corrupt:{reference.event_id}"
            references = (
                CommittedResultEventReference(
                    event_id=corrupted_id,
                    sequence_no=reference.sequence_no,
                    event_type=reference.event_type,
                    stored_event_reference=corrupted_id,
                ),
                *references[1:],
            )
            result_event_ids = (corrupted_id, *result_event_ids[1:])
        elif self.receipt_corruption == "event_type":
            reference = references[0]
            references = (
                replace(
                    reference,
                    event_type=GameEventType.SESSION_CONTROL_REJECTED,
                ),
                *references[1:],
            )
        elif self.receipt_corruption == "event_sequence":
            reference = references[0]
            references = (
                replace(reference, sequence_no=plan.input_sequence_no),
                *references[1:],
            )
        elif self.receipt_corruption == "stored_event_reference":
            reference = references[0]
            forged = object.__new__(CommittedResultEventReference)
            object.__setattr__(forged, "event_id", reference.event_id)
            object.__setattr__(forged, "sequence_no", reference.sequence_no)
            object.__setattr__(forged, "event_type", reference.event_type)
            object.__setattr__(
                forged,
                "stored_event_reference",
                f"corrupt:{reference.stored_event_reference}",
            )
            references = (forged, *references[1:])
        elif self.receipt_corruption == "operation_status":
            operation_status = ControlOperationStatus.FAILED
        elif self.receipt_corruption == "ownership_generation":
            ownership_generation = 4
        return ControlApplyReceipt(
            game_id=plan.game_id,
            session_id=plan.session_id,
            committed_state_version=committed_version,
            committed_cursor=plan.input_sequence_no,
            result_event_ids=result_event_ids,
            operation_status=operation_status,
            ownership_generation=ownership_generation,
            commit_evidence_reference=f"atomic-commit:{plan.operation_id}",
            command_id=plan.command_id,
            operation_id=plan.operation_id,
            operation_claim_id=claim.claim_id,
            input_event_id=plan.input_event_id,
            input_sequence_no=plan.input_sequence_no,
            result_event_references=references,
        )


class StateBoundEvidenceFactory:
    """P5-1 adapter fake freezing evidence from the Actor's current state.

    Production ingress wiring remains Phase 6 scope. This adapter constructs
    only ActorValidatedControlTurnEvidence; it never prebuilds outcomes,
    receipts, or commits.
    """

    __slots__ = ("build_calls", "evidence", "turns")

    def __init__(
        self,
        turns: dict[str, tuple[SessionCommandType, GamePhase]],
    ) -> None:
        self.turns = turns
        self.build_calls: list[
            tuple[ActorVisibleGameState, ControlEventDeliveryEnvelope]
        ] = []
        self.evidence: list[ActorValidatedControlTurnEvidence] = []

    def build(
        self,
        *,
        state: ActorVisibleGameState,
        envelope: ControlEventDeliveryEnvelope,
    ) -> ActorValidatedControlTurnEvidence:
        self.build_calls.append((state, envelope))
        command_type, target_phase = self.turns[envelope.event.event_id]
        snapshot = state.snapshot
        context = make_context(
            command_type,
            status=snapshot.status,
            current_phase=snapshot.current_phase,
            target_phase=target_phase,
            session_version=snapshot.state_version,
            command_observed_version=envelope.observed_state_version,
            active_generation=state.ownership_generation,
        )
        session_view = replace(
            context.session_view,
            status=snapshot.status,
            current_phase=snapshot.current_phase,
            state_version=snapshot.state_version,
            last_applied_sequence_no=state.committed_control_cursor,
            current_game_snapshot=snapshot,
        )
        ownership = replace(
            context.ownership_evidence,
            active_generation=state.ownership_generation,
            observed_state_version=snapshot.state_version,
        )
        evidence = ActorValidatedControlTurnEvidence(
            envelope=envelope,
            command_intent=context.command_intent,
            session_view=session_view,
            participant_views=context.participant_views,
            setup_view=context.setup_view,
            ownership_evidence=ownership,
            governance_schema_version=1,
            authorization_reference=envelope.authorization_reference,
            confirmation_reference=envelope.confirmation_reference,
            lifecycle_evidence=context.lifecycle_evidence,
            setup_participant_evidence=context.setup_participant_evidence,
            claim_attempt=ControlClaimAttemptEvidence.from_envelope(
                envelope,
                claimed_at=context.claim.claimed_at,
            ),
        )
        self.evidence.append(evidence)
        return evidence


def _initial_state(
    *,
    phase: GamePhase = GamePhase.EXPLORATION,
) -> ActorVisibleGameState:
    context = make_context(
        SessionCommandType.CHANGE_PHASE,
        status=GameSessionStatus.RUNNING,
        current_phase=phase,
        target_phase=(
            GamePhase.ENDING
            if phase is GamePhase.VOTING
            else GamePhase.DISCUSSION
        ),
        active_generation=3,
    )
    snapshot = _current_snapshot(context)
    return ActorVisibleGameState(
        snapshot=snapshot,
        committed_control_cursor=snapshot.last_applied_sequence_no,
        ownership_generation=3,
        last_completion_identity=None,
    )


def _envelope(
    *,
    sequence: int,
    command_type: SessionCommandType,
    status: GameSessionStatus,
    current_phase: GamePhase,
    observed_state_version: int,
    target_phase: GamePhase = GamePhase.DISCUSSION,
) -> ControlEventDeliveryEnvelope:
    context = make_context(
        command_type,
        status=status,
        current_phase=current_phase,
        target_phase=target_phase,
        session_version=observed_state_version,
        command_observed_version=observed_state_version,
        active_generation=3,
    )
    command_id = f"command-{sequence}"
    operation_id = f"operation-{sequence}"
    event_id = f"event-{sequence}"
    request_event_id = f"request-event-{sequence}"
    correlation_id = f"correlation-{sequence}"
    fingerprint = context.command_intent.payload_fingerprint
    payload = DMCommandEventPayload(
        command_id=command_id,
        command_type=command_type,
        requester="participant-dm",
        causation_event_id=request_event_id,
        observed_state_version=observed_state_version,
        payload_reference=f"sha256:{fingerprint}",
        payload_fingerprint=fingerprint,
    )
    event = replace(
        context.envelope.event,
        event_id=event_id,
        correlation_id=correlation_id,
        payload=payload.to_mapping(),
        observed_state_version=observed_state_version,
        causation_event_id=request_event_id,
    )
    return ControlEventDeliveryEnvelope(
        event=event,
        event_sequence_no=sequence,
        operation_id=operation_id,
        command_id=command_id,
        observed_state_version=observed_state_version,
        requester_principal_ref="participant-dm",
        requester_binding_version=2,
        authorization_reference=f"authorization-{sequence}",
        confirmation_reference=f"confirmation-{sequence}",
        correlation_id=correlation_id,
        stored_event_reference=event_id,
    )


def _runtime(
    *,
    initial_state: ActorVisibleGameState,
    envelopes: tuple[ControlEventDeliveryEnvelope, ...],
    targets: tuple[GamePhase, ...],
    failure_mode: str | None = None,
    receipt_corruption: str | None = None,
    block_first_claim: bool = False,
) -> tuple[
    StrictRecordingAtomicApplyPort,
    CompositeGameControlApplyPlanBuilder,
    ActorOwnedApplyCoordinator,
    ControlTurnProcessor,
    StateBoundEvidenceFactory,
    GameSessionActor,
    SessionAsyncGate,
]:
    turns = {
        envelope.event.event_id: (
            SessionCommandType(
                envelope.event.payload["command_type"]
            ),
            target,
        )
        for envelope, target in zip(envelopes, targets)
    }
    port = StrictRecordingAtomicApplyPort(
        ownership_generation=initial_state.ownership_generation,
        failure_mode=failure_mode,
        receipt_corruption=receipt_corruption,
        block_first_claim=block_first_claim,
    )
    dispatcher = CompositeGameControlApplyPlanBuilder()
    coordinator = ActorOwnedApplyCoordinator(
        apply_port=port,
        builder=dispatcher,
    )
    processor = ControlTurnProcessor(coordinator)
    factory = StateBoundEvidenceFactory(turns)
    actor = GameSessionActor.for_composite_control(
        session_projection_seed=make_session(),
        initial_state=initial_state,
        control_turn_evidence_factory=factory,
        control_turn_processor=processor,
    )
    gate = SessionAsyncGate(
        game_id=initial_state.snapshot.game_id,
        session_id=initial_state.snapshot.session_id,
        next_expected_sequence_no=envelopes[0].event_sequence_no,
        consumer=actor,
        max_queue_size=max(1, len(envelopes)),
    )
    return port, dispatcher, coordinator, processor, factory, actor, gate


def _owned_async_queues(*owners: object) -> tuple[asyncio.Queue[object], ...]:
    queues: list[asyncio.Queue[object]] = []
    for owner in owners:
        values = list(getattr(owner, "__dict__", {}).values())
        for owner_type in type(owner).__mro__:
            slots = getattr(owner_type, "__slots__", ())
            if isinstance(slots, str):
                slots = (slots,)
            values.extend(
                getattr(owner, slot)
                for slot in slots
                if slot not in {"__dict__", "__weakref__"}
                and hasattr(owner, slot)
            )
        queues.extend(
            value for value in values if isinstance(value, asyncio.Queue)
        )
    return tuple(dict.fromkeys(queues))


async def _drive(
    gate: SessionAsyncGate,
    envelopes: tuple[ControlEventDeliveryEnvelope, ...],
) -> None:
    await gate.start()
    for envelope in envelopes:
        assert await gate.admit(envelope) is GateAdmission.ACCEPTED
    await gate.close()


def test_valid_change_phase_commits_through_one_real_control_plane() -> None:
    initial = _initial_state()
    before = initial.snapshot
    envelope = _envelope(
        sequence=7,
        command_type=SessionCommandType.CHANGE_PHASE,
        status=GameSessionStatus.RUNNING,
        current_phase=GamePhase.EXPLORATION,
        observed_state_version=4,
        target_phase=GamePhase.DISCUSSION,
    )
    (
        port,
        dispatcher,
        coordinator,
        processor,
        factory,
        actor,
        gate,
    ) = _runtime(
        initial_state=initial,
        envelopes=(envelope,),
        targets=(GamePhase.DISCUSSION,),
    )

    asyncio.run(_drive(gate, (envelope,)))

    state = actor.visible_game_state
    assert gate.lifecycle is GateLifecycle.CLOSED
    assert [call[0] for call in port.calls] == [
        "claim_operation",
        "commit_control_apply",
    ]
    assert processor._coordinator is coordinator
    assert coordinator._builder is dispatcher
    assert gate._consumer is actor
    assert isinstance(
        actor._game_state_completion_boundary,
        ActorOwnedGameStateCompletionBoundary,
    )
    assert _owned_async_queues(
        port,
        dispatcher,
        coordinator,
        processor,
        factory,
        actor,
        gate,
    ) == (gate._queue,)
    assert isinstance(actor._mailbox, deque)
    assert tuple(actor._mailbox) == ()
    assert actor.processed_event_ids == ()
    assert len(factory.build_calls) == 1
    assert factory.build_calls[0] == (initial, envelope)
    assert factory.evidence[0].envelope is envelope
    assert (
        factory.evidence[0].session_view.current_game_snapshot
        is initial.snapshot
    )
    assert state.snapshot.current_phase is GamePhase.DISCUSSION
    assert state.snapshot.phase.phase is GamePhase.DISCUSSION
    assert state.snapshot.state_version == before.state_version + 1
    assert state.snapshot.phase.domain_version == before.phase.domain_version + 1
    assert state.snapshot.last_applied_sequence_no == 7
    assert state.committed_control_cursor == 7
    assert state.ownership_generation == 3
    assert state.snapshot.lifecycle is before.lifecycle
    assert state.snapshot.setup is before.setup
    assert state.snapshot.participants is before.participants
    assert state.snapshot.game_rules is before.game_rules
    assert state.snapshot.hidden_state is before.hidden_state
    assert state.last_completion_identity is not None
    assert (
        state.last_completion_identity.completion_kind
        is ControlCompletionKind.APPLIED
    )
    applied_plan = port.calls[1][1]
    assert isinstance(applied_plan, ControlApplyPlan)
    assert (
        applied_plan.ownership_intent.intent_type
        is OwnershipIntentType.RETAIN
    )
    assert tuple(event.event_type for event in applied_plan.result_events) == (
        GameEventType.PHASE_CHANGED,
    )


def test_invalid_phase_business_reject_only_advances_control_cursor() -> None:
    initial = _initial_state()
    before = initial.snapshot
    envelope = _envelope(
        sequence=7,
        command_type=SessionCommandType.CHANGE_PHASE,
        status=GameSessionStatus.RUNNING,
        current_phase=GamePhase.EXPLORATION,
        observed_state_version=4,
        target_phase=GamePhase.VOTING,
    )
    port, _, _, _, factory, actor, gate = _runtime(
        initial_state=initial,
        envelopes=(envelope,),
        targets=(GamePhase.VOTING,),
    )

    asyncio.run(_drive(gate, (envelope,)))

    state = actor.visible_game_state
    assert gate.lifecycle is GateLifecycle.CLOSED
    assert [call[0] for call in port.calls] == [
        "claim_operation",
        "commit_control_rejection",
    ]
    rejected_plan = port.calls[1][1]
    assert isinstance(rejected_plan, ControlRejectPlan)
    rejected_payload = validate_control_result_event(
        rejected_plan.rejection_event
    )
    assert isinstance(rejected_payload, SessionControlRejectedPayload)
    assert rejected_payload.reason_code == "INVALID_PHASE_TRANSITION"
    assert state.snapshot is before
    assert state.snapshot.state_version == 4
    assert state.snapshot.last_applied_sequence_no == 6
    assert state.committed_control_cursor == 7
    assert state.ownership_generation == 3
    assert state.last_completion_identity is not None
    assert (
        state.last_completion_identity.completion_kind
        is ControlCompletionKind.REJECTED
    )


def test_voting_to_ending_keeps_running_then_end_game_uses_same_dispatcher() -> None:
    initial = _initial_state(phase=GamePhase.VOTING)
    phase_envelope = _envelope(
        sequence=7,
        command_type=SessionCommandType.CHANGE_PHASE,
        status=GameSessionStatus.RUNNING,
        current_phase=GamePhase.VOTING,
        observed_state_version=4,
        target_phase=GamePhase.ENDING,
    )
    end_envelope = _envelope(
        sequence=8,
        command_type=SessionCommandType.END_GAME,
        status=GameSessionStatus.RUNNING,
        current_phase=GamePhase.ENDING,
        observed_state_version=5,
        target_phase=GamePhase.ENDING,
    )
    envelopes = (phase_envelope, end_envelope)
    port, dispatcher, coordinator, processor, factory, actor, gate = _runtime(
        initial_state=initial,
        envelopes=envelopes,
        targets=(GamePhase.ENDING, GamePhase.ENDING),
    )

    asyncio.run(_drive(gate, envelopes))

    assert gate.lifecycle is GateLifecycle.CLOSED
    assert processor._coordinator is coordinator
    assert coordinator._builder is dispatcher
    assert len(factory.build_calls) == 2
    after_phase = factory.build_calls[1][0]
    assert factory.build_calls == [
        (initial, phase_envelope),
        (after_phase, end_envelope),
    ]
    assert after_phase.snapshot.current_phase is GamePhase.ENDING
    assert after_phase.snapshot.status is GameSessionStatus.RUNNING
    assert after_phase.snapshot.phase.domain_version == (
        initial.snapshot.phase.domain_version + 1
    )
    final = actor.visible_game_state
    assert final.snapshot.status is GameSessionStatus.ENDED
    assert final.snapshot.current_phase is GamePhase.ENDING
    assert final.snapshot.lifecycle.domain_version == (
        initial.snapshot.lifecycle.domain_version + 1
    )
    assert final.snapshot.phase is after_phase.snapshot.phase
    assert final.snapshot.state_version == 6
    assert final.committed_control_cursor == 8
    assert [call[0] for call in port.calls] == [
        "claim_operation",
        "commit_control_apply",
        "claim_operation",
        "commit_control_apply",
    ]
    plans = [port.calls[1][1], port.calls[3][1]]
    assert all(isinstance(plan, ControlApplyPlan) for plan in plans)
    assert plans[0].command_type is SessionCommandType.CHANGE_PHASE
    assert plans[1].command_type is SessionCommandType.END_GAME
    assert (
        plans[1].ownership_intent.intent_type
        is OwnershipIntentType.RELEASE
    )
    assert plans[1].ownership_intent.expected_generation == 3
    assert plans[1].ownership_intent.resulting_generation is None
    assert final.ownership_generation is None
    assert isinstance(actor._mailbox, deque)
    assert tuple(actor._mailbox) == ()
    assert actor.processed_event_ids == ()


@pytest.mark.parametrize(
    ("command_type", "failure_mode", "expected_reason", "expected_calls"),
    [
        (
            SessionCommandType.RESUME_GAME,
            None,
            ControlTurnFailureReason.BUILD_NON_COMMIT,
            ["claim_operation"],
        ),
        (
            SessionCommandType.CHANGE_PHASE,
            "invalid_receipt",
            ControlTurnFailureReason.RECEIPT_INVALID,
            ["claim_operation", "commit_control_apply"],
        ),
        (
            SessionCommandType.CHANGE_PHASE,
            "port_failure",
            ControlTurnFailureReason.APPLY_UNKNOWN,
            ["claim_operation", "commit_control_apply"],
        ),
    ],
)
def test_noncommit_receipt_and_port_failures_fault_stop_without_visibility(
    command_type: SessionCommandType,
    failure_mode: str | None,
    expected_reason: ControlTurnFailureReason,
    expected_calls: list[str],
) -> None:
    initial = _initial_state()
    target = GamePhase.DISCUSSION
    first = _envelope(
        sequence=7,
        command_type=command_type,
        status=GameSessionStatus.RUNNING,
        current_phase=GamePhase.EXPLORATION,
        observed_state_version=4,
        target_phase=target,
    )
    second = _envelope(
        sequence=8,
        command_type=SessionCommandType.CHANGE_PHASE,
        status=GameSessionStatus.RUNNING,
        current_phase=GamePhase.EXPLORATION,
        observed_state_version=4,
        target_phase=GamePhase.DISCUSSION,
    )
    port, _, _, _, factory, actor, gate = _runtime(
        initial_state=initial,
        envelopes=(first, second),
        targets=(target, GamePhase.DISCUSSION),
        failure_mode=failure_mode,
        block_first_claim=True,
    )

    async def scenario() -> None:
        await gate.start()
        first_admission = asyncio.create_task(gate.admit(first))
        await port.first_claim_started.wait()
        assert await first_admission is GateAdmission.ACCEPTED
        assert await gate.admit(second) is GateAdmission.ACCEPTED
        assert gate.pending_count == 1
        port.release_first_claim.set()
        await gate.wait_faulted()

    asyncio.run(scenario())

    assert gate.lifecycle is GateLifecycle.FAULTED
    assert isinstance(gate.fault, ControlTurnProcessingError)
    assert gate.fault.reason is expected_reason
    assert gate.failed_envelope is first
    assert actor.visible_game_state is initial
    assert [call[0] for call in port.calls] == expected_calls
    assert [envelope for _, envelope in factory.build_calls] == [first]
    assert port.calls[0][1]["input_event_id"] == first.event.event_id
    if len(port.calls) == 2:
        committed_plan = port.calls[1][1]
        assert isinstance(committed_plan, ControlApplyPlan)
        assert committed_plan.input_event_id == first.event.event_id
    # The frozen Gate retains the queued second turn after faulting, but its
    # stopped consumer never exposes that turn to the Actor-owned chain.
    assert gate.pending_count == 1
    assert actor.visible_game_state.committed_control_cursor == 6
    assert tuple(actor._mailbox) == ()
    assert actor.processed_event_ids == ()


@pytest.mark.parametrize(
    "receipt_corruption",
    [
        "event_id",
        "event_type",
        "event_sequence",
        "stored_event_reference",
        "operation_status",
        "ownership_generation",
    ],
)
def test_receipt_corruption_fault_stops_before_state_visibility(
    receipt_corruption: str,
) -> None:
    initial = _initial_state()
    envelope = _envelope(
        sequence=7,
        command_type=SessionCommandType.CHANGE_PHASE,
        status=GameSessionStatus.RUNNING,
        current_phase=GamePhase.EXPLORATION,
        observed_state_version=4,
        target_phase=GamePhase.DISCUSSION,
    )
    port, _, _, _, factory, actor, gate = _runtime(
        initial_state=initial,
        envelopes=(envelope,),
        targets=(GamePhase.DISCUSSION,),
        receipt_corruption=receipt_corruption,
    )

    async def scenario() -> None:
        await gate.start()
        assert await gate.admit(envelope) is GateAdmission.ACCEPTED
        await gate.wait_faulted()

    asyncio.run(scenario())

    assert gate.lifecycle is GateLifecycle.FAULTED
    assert isinstance(gate.fault, ControlTurnProcessingError)
    assert gate.fault.reason is ControlTurnFailureReason.RECEIPT_INVALID
    assert gate.failed_envelope is envelope
    assert factory.build_calls == [(initial, envelope)]
    assert [call[0] for call in port.calls] == [
        "claim_operation",
        "commit_control_apply",
    ]
    assert actor.visible_game_state is initial
    assert actor.visible_game_state.committed_control_cursor == 6


def test_contract_adapters_have_no_io_or_second_control_plane() -> None:
    source = inspect.getsource(inspect.getmodule(_runtime))
    tree = ast.parse(source)
    imports = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    method_names = {
        node.name
        for node in ast.walk(
            ast.parse(inspect.getsource(StrictRecordingAtomicApplyPort))
        )
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    forbidden_direct_import_terms = {
        "persistence",
        "event_store",
        "eventstore",
        "recovery",
        "notification",
        "database",
        "sqlite",
    }

    assert not any(
        term in imported.casefold()
        for imported in imports
        for term in forbidden_direct_import_terms
    )
    assert method_names == {
        "__init__",
        "claim_operation",
        "commit_control_apply",
        "commit_control_rejection",
        "_receipt",
    }
    assert {
        "append_event",
        "append_events",
        "notify",
        "send",
        "recover",
    }.isdisjoint(method_names)
    port_tree = ast.parse(inspect.getsource(StrictRecordingAtomicApplyPort))
    port_calls = {
        (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
        )
        for node in ast.walk(port_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Name, ast.Attribute))
    }
    external_io_calls = {
        "append_event",
        "append_events",
        "notify",
        "send",
        "recover",
        "open",
        "connect",
        "execute",
    }
    assert external_io_calls.isdisjoint(port_calls)
    adapter_source = inspect.getsource(StateBoundEvidenceFactory)
    adapter_tree = ast.parse(adapter_source)
    adapter_calls = {
        (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
        )
        for node in ast.walk(adapter_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Name, ast.Attribute))
    }
    assert {
        "BuildPlanReady",
        "BuildReject",
        "ControlApplyReceipt",
        "commit_control_apply",
        "commit_control_rejection",
        "append_event",
        "notify",
        "send",
        *external_io_calls,
    }.isdisjoint(adapter_calls)
    module_helpers = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("_")
    ]
    helper_calls = {
        (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
        )
        for helper in module_helpers
        for node in ast.walk(helper)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Name, ast.Attribute))
    }
    assert external_io_calls.isdisjoint(helper_calls)
    runtime_tree = ast.parse(inspect.getsource(_runtime))
    runtime_calls = [
        node.func.id
        for node in ast.walk(runtime_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    runtime_attribute_calls = [
        f"{node.func.value.id}.{node.func.attr}"
        for node in ast.walk(runtime_tree)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
        )
    ]
    assert runtime_calls.count("CompositeGameControlApplyPlanBuilder") == 1
    assert runtime_calls.count("ActorOwnedApplyCoordinator") == 1
    assert runtime_calls.count("ControlTurnProcessor") == 1
    assert (
        runtime_attribute_calls.count("GameSessionActor.for_composite_control")
        == 1
    )
    assert runtime_calls.count("SessionAsyncGate") == 1
