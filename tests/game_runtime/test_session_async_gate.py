from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from importlib import import_module

import pytest

from game_runtime.event import EventVisibility, GameEvent, GameEventSource, GameEventType
from game_runtime.session_control.delivery import ControlEventDeliveryEnvelope
from game_runtime.session_control.event_integration import DMCommandEventPayload
from game_runtime.session_control.commands import SessionCommandType


NOW = datetime(2026, 7, 16, 13, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def gate_module():
    return import_module("game_runtime.actor.async_gate")


def make_envelope(
    sequence_no: int,
    *,
    game_id: str = "game-1",
    session_id: str = "session-1",
) -> ControlEventDeliveryEnvelope:
    suffix = f"{game_id}-{session_id}-{sequence_no}"
    payload = DMCommandEventPayload(
        command_id=f"command-{suffix}",
        command_type=SessionCommandType.PAUSE_GAME,
        requester="principal-dm-1",
        causation_event_id=f"message-{suffix}",
        observed_state_version=sequence_no - 1,
        payload_reference="sha256:" + "a" * 64,
        payload_fingerprint="a" * 64,
    )
    event = GameEvent(
        event_id=f"event-{suffix}",
        game_id=game_id,
        session_id=session_id,
        event_type=GameEventType.DM_COMMAND,
        actor="principal-dm-1",
        source=GameEventSource.CONTROL,
        correlation_id=f"correlation-{suffix}",
        timestamp=NOW,
        payload=payload.to_mapping(),
        visibility=EventVisibility.DM_CONTROL,
        observed_state_version=sequence_no - 1,
        causation_event_id=f"message-{suffix}",
    )
    return ControlEventDeliveryEnvelope(
        event=event,
        event_sequence_no=sequence_no,
        operation_id=f"operation-{suffix}",
        command_id=f"command-{suffix}",
        observed_state_version=sequence_no - 1,
        requester_principal_ref="principal-dm-1",
        requester_binding_version=1,
        authorization_reference=f"authorization-{suffix}",
        confirmation_reference=None,
        correlation_id=f"correlation-{suffix}",
        stored_event_reference=f"event-{suffix}",
    )


class RecordingConsumer:
    def __init__(self) -> None:
        self.sequences: list[int] = []
        self.active = 0
        self.max_active = 0

    async def handle_control_turn(
        self,
        envelope: ControlEventDeliveryEnvelope,
    ) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0)
        self.sequences.append(envelope.event_sequence_no)
        self.active -= 1


class BlockingConsumer(RecordingConsumer):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def handle_control_turn(
        self,
        envelope: ControlEventDeliveryEnvelope,
    ) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        await self.release.wait()
        self.sequences.append(envelope.event_sequence_no)
        self.active -= 1


def make_gate(consumer, **overrides):
    values = {
        "game_id": "game-1",
        "session_id": "session-1",
        "next_expected_sequence_no": 1,
        "consumer": consumer,
        "max_queue_size": 8,
    }
    values.update(overrides)
    return gate_module().SessionAsyncGate(**values)


def test_delivers_envelopes_in_fifo_sequence_order() -> None:
    async def scenario() -> None:
        consumer = RecordingConsumer()
        gate = make_gate(consumer)
        await gate.start()

        for sequence_no in (1, 2, 3):
            result = await gate.admit(make_envelope(sequence_no))
            assert result is gate_module().GateAdmission.ACCEPTED

        await gate.close()
        assert consumer.sequences == [1, 2, 3]

    run(scenario())


def test_same_session_turns_never_execute_concurrently() -> None:
    async def scenario() -> None:
        consumer = BlockingConsumer()
        gate = make_gate(consumer)
        await gate.start()
        await gate.admit(make_envelope(1))
        await asyncio.wait_for(consumer.entered.wait(), timeout=1)
        await gate.admit(make_envelope(2))

        assert consumer.sequences == []
        assert consumer.max_active == 1

        consumer.release.set()
        await gate.close()
        assert consumer.sequences == [1, 2]
        assert consumer.max_active == 1

    run(scenario())


def test_different_session_gates_can_execute_in_parallel() -> None:
    async def scenario() -> None:
        first = BlockingConsumer()
        second = BlockingConsumer()
        first_gate = make_gate(first)
        second_gate = make_gate(
            second,
            game_id="game-2",
            session_id="session-2",
        )
        await asyncio.gather(first_gate.start(), second_gate.start())

        await asyncio.gather(
            first_gate.admit(make_envelope(1)),
            second_gate.admit(
                make_envelope(1, game_id="game-2", session_id="session-2")
            ),
        )
        await asyncio.wait_for(
            asyncio.gather(first.entered.wait(), second.entered.wait()),
            timeout=1,
        )

        assert first.active == 1
        assert second.active == 1

        first.release.set()
        second.release.set()
        await asyncio.gather(first_gate.close(), second_gate.close())

    run(scenario())


def test_duplicate_identity_is_admitted_only_once() -> None:
    async def scenario() -> None:
        consumer = RecordingConsumer()
        gate = make_gate(consumer)
        envelope = make_envelope(1)
        await gate.start()

        assert await gate.admit(envelope) is gate_module().GateAdmission.ACCEPTED
        assert await gate.admit(envelope) is gate_module().GateAdmission.DUPLICATE

        await gate.close()
        assert consumer.sequences == [1]

    run(scenario())


def test_sequence_gap_is_rejected_without_reordering_or_faulting() -> None:
    async def scenario() -> None:
        gate = make_gate(RecordingConsumer())
        await gate.start()

        with pytest.raises(gate_module().SequenceGapError):
            await gate.admit(make_envelope(2))

        assert gate.lifecycle is gate_module().GateLifecycle.RUNNING
        assert gate.pending_count == 0
        await gate.close()

    run(scenario())


def test_same_sequence_with_different_identity_faults_the_gate() -> None:
    async def scenario() -> None:
        consumer = BlockingConsumer()
        gate = make_gate(consumer)
        envelope = make_envelope(1)
        await gate.start()
        await gate.admit(envelope)
        await asyncio.wait_for(consumer.entered.wait(), timeout=1)

        conflict = replace(envelope, operation_id="operation-conflict")
        with pytest.raises(gate_module().SequenceConflictError):
            await gate.admit(conflict)

        assert gate.lifecycle is gate_module().GateLifecycle.FAULTED
        consumer.release.set()
        await gate.wait_faulted()

    run(scenario())


def test_lifecycle_runs_through_graceful_closing() -> None:
    async def scenario() -> None:
        consumer = BlockingConsumer()
        gate = make_gate(consumer)
        assert gate.lifecycle is gate_module().GateLifecycle.NEW

        await gate.start()
        assert gate.lifecycle is gate_module().GateLifecycle.RUNNING
        await gate.admit(make_envelope(1))
        await asyncio.wait_for(consumer.entered.wait(), timeout=1)

        closing = asyncio.create_task(gate.close())
        await asyncio.sleep(0)
        assert gate.lifecycle is gate_module().GateLifecycle.CLOSING
        assert not closing.done()

        consumer.release.set()
        await closing
        assert gate.lifecycle is gate_module().GateLifecycle.CLOSED

    run(scenario())


def test_consumer_failure_faults_lane_and_preserves_unprocessed_work() -> None:
    class FailingConsumer:
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def handle_control_turn(
            self,
            envelope: ControlEventDeliveryEnvelope,
        ) -> None:
            self.entered.set()
            await self.release.wait()
            raise RuntimeError("consumer failed")

    async def scenario() -> None:
        consumer = FailingConsumer()
        gate = make_gate(consumer)
        first = make_envelope(1)
        second = make_envelope(2)
        await gate.start()
        await gate.admit(first)
        await asyncio.wait_for(consumer.entered.wait(), timeout=1)
        await gate.admit(second)

        consumer.release.set()
        await gate.wait_faulted()

        assert gate.lifecycle is gate_module().GateLifecycle.FAULTED
        assert isinstance(gate.fault, RuntimeError)
        assert gate.failed_envelope is first
        assert gate.pending_count == 1
        with pytest.raises(gate_module().GateNotAcceptingError):
            await gate.admit(make_envelope(3))

    run(scenario())


def test_successful_queue_put_remains_an_accepted_admission_if_consumer_faults() -> None:
    class ImmediateFailingConsumer:
        async def handle_control_turn(
            self,
            envelope: ControlEventDeliveryEnvelope,
        ) -> None:
            raise RuntimeError("immediate consumer failure")

    async def scenario() -> None:
        gate = make_gate(ImmediateFailingConsumer())
        envelope = make_envelope(1)
        await gate.start()

        admission = await gate.admit(envelope)
        await gate.wait_faulted()

        assert admission is gate_module().GateAdmission.ACCEPTED
        assert gate.failed_envelope is envelope

    run(scenario())


def test_full_queue_applies_backpressure_without_silent_drop() -> None:
    async def scenario() -> None:
        consumer = BlockingConsumer()
        gate = make_gate(consumer, max_queue_size=1)
        await gate.start()
        await gate.admit(make_envelope(1))
        await asyncio.wait_for(consumer.entered.wait(), timeout=1)
        await gate.admit(make_envelope(2))

        third_admission = asyncio.create_task(gate.admit(make_envelope(3)))
        await asyncio.sleep(0)
        assert not third_admission.done()
        assert gate.pending_count == 1

        consumer.release.set()
        assert await asyncio.wait_for(third_admission, timeout=1) is (
            gate_module().GateAdmission.ACCEPTED
        )
        await gate.close()
        assert consumer.sequences == [1, 2, 3]

    run(scenario())


def test_rejects_envelope_for_another_session() -> None:
    async def scenario() -> None:
        gate = make_gate(RecordingConsumer())
        await gate.start()

        with pytest.raises(gate_module().GateScopeError):
            await gate.admit(
                make_envelope(1, game_id="game-other", session_id="session-other")
            )

        assert gate.lifecycle is gate_module().GateLifecycle.RUNNING
        assert gate.pending_count == 0
        await gate.close()

    run(scenario())
