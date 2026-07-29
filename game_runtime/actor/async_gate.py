"""Per-session async admission gate for persisted Control Events."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from enum import Enum
from typing import Protocol, cast

from game_runtime.session_control.delivery import ControlEventDeliveryEnvelope


class GateLifecycle(str, Enum):
    NEW = "NEW"
    RUNNING = "RUNNING"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    FAULTED = "FAULTED"


class GateAdmission(str, Enum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"


class SessionAsyncGateError(RuntimeError):
    """Base error for async Gate contract violations."""


class GateLifecycleError(SessionAsyncGateError):
    """Raised when a lifecycle transition is not allowed."""


class GateNotAcceptingError(SessionAsyncGateError):
    """Raised when admission is attempted outside RUNNING."""


class GateScopeError(SessionAsyncGateError):
    """Raised when an Envelope targets a different Session."""


class SequenceAdmissionError(SessionAsyncGateError):
    """Base error for persisted sequence admission failures."""


class SequenceGapError(SequenceAdmissionError):
    """Raised when admission would skip the next persisted sequence."""


class SequenceConflictError(SequenceAdmissionError):
    """Raised when one sequence is bound to conflicting identities."""


class GateFaultedError(SessionAsyncGateError):
    """Raised when graceful completion is interrupted by a Gate fault."""


class AsyncControlTurnConsumer(Protocol):
    async def handle_control_turn(
        self,
        envelope: ControlEventDeliveryEnvelope,
    ) -> None:
        """Run one Actor-owned turn and return before the next may begin."""


_STOP = object()
DeliveryIdentity = tuple[int, str, str, str]


class SessionAsyncGate:
    """Own one FIFO consumer lane for exactly one game/session scope."""

    def __init__(
        self,
        *,
        game_id: str,
        session_id: str,
        next_expected_sequence_no: int,
        consumer: AsyncControlTurnConsumer,
        max_queue_size: int,
    ) -> None:
        _require_text("game_id", game_id)
        _require_text("session_id", session_id)
        _require_positive_int(
            "next_expected_sequence_no", next_expected_sequence_no
        )
        _require_positive_int("max_queue_size", max_queue_size)
        if not callable(getattr(consumer, "handle_control_turn", None)):
            raise TypeError("consumer must define handle_control_turn")

        self._game_id = game_id
        self._session_id = session_id
        self._next_expected_sequence_no = next_expected_sequence_no
        self._consumer = consumer
        self._queue: asyncio.Queue[object] = asyncio.Queue(
            maxsize=max_queue_size
        )
        self._lifecycle = GateLifecycle.NEW
        self._admission_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._consumer_task: asyncio.Task[None] | None = None
        self._identities_by_sequence: dict[int, DeliveryIdentity] = {}
        self._in_flight: ControlEventDeliveryEnvelope | None = None
        self._failed_envelope: ControlEventDeliveryEnvelope | None = None
        self._fault: BaseException | None = None
        self._faulted = asyncio.Event()

    @property
    def game_id(self) -> str:
        return self._game_id

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def lifecycle(self) -> GateLifecycle:
        return self._lifecycle

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()

    @property
    def fault(self) -> BaseException | None:
        return self._fault

    @property
    def failed_envelope(self) -> ControlEventDeliveryEnvelope | None:
        return self._failed_envelope

    async def start(self) -> None:
        async with self._admission_lock:
            if self._lifecycle is not GateLifecycle.NEW:
                raise GateLifecycleError("Gate can only start from NEW")
            self._lifecycle = GateLifecycle.RUNNING
            self._consumer_task = asyncio.create_task(
                self._consume(),
                name=f"game-session-gate:{self._game_id}:{self._session_id}",
            )

    async def admit(
        self,
        envelope: ControlEventDeliveryEnvelope,
    ) -> GateAdmission:
        if not isinstance(envelope, ControlEventDeliveryEnvelope):
            raise TypeError("envelope must be a ControlEventDeliveryEnvelope")

        async with self._admission_lock:
            self._require_accepting()
            self._validate_scope(envelope)
            sequence_no = envelope.event_sequence_no
            identity = _delivery_identity(envelope)
            existing_identity = self._identities_by_sequence.get(sequence_no)

            if existing_identity is not None:
                if existing_identity == identity:
                    return GateAdmission.DUPLICATE
                error = SequenceConflictError(
                    "persisted sequence is bound to a different delivery identity"
                )
                self._enter_fault(error)
                raise error
            if sequence_no < self._next_expected_sequence_no:
                error = SequenceConflictError(
                    "persisted sequence precedes the Gate admission watermark"
                )
                self._enter_fault(error)
                raise error
            if sequence_no > self._next_expected_sequence_no:
                raise SequenceGapError(
                    "persisted sequence would skip the next expected sequence"
                )

            await self._put_with_fault_stop(envelope)
            self._identities_by_sequence[sequence_no] = identity
            self._next_expected_sequence_no += 1
            return GateAdmission.ACCEPTED

    async def close(self) -> None:
        async with self._close_lock:
            async with self._admission_lock:
                if self._lifecycle is GateLifecycle.CLOSED:
                    return
                if self._lifecycle is not GateLifecycle.RUNNING:
                    raise GateLifecycleError(
                        "Gate can only close gracefully from RUNNING"
                    )
                self._lifecycle = GateLifecycle.CLOSING
                consumer_task = self._require_consumer_task()

            join_task = asyncio.create_task(self._queue.join())
            done, _ = await asyncio.wait(
                {join_task, consumer_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if consumer_task in done and self._lifecycle is GateLifecycle.FAULTED:
                join_task.cancel()
                with suppress(asyncio.CancelledError):
                    await join_task
                raise GateFaultedError("Gate faulted before queued work drained")

            await join_task
            self._queue.put_nowait(_STOP)
            await consumer_task
            if self._lifecycle is GateLifecycle.FAULTED:
                raise GateFaultedError("Gate faulted while closing")
            self._lifecycle = GateLifecycle.CLOSED

    async def wait_faulted(self) -> None:
        await self._faulted.wait()
        consumer_task = self._consumer_task
        if consumer_task is not None and consumer_task is not asyncio.current_task():
            with suppress(asyncio.CancelledError):
                await consumer_task

    async def _put_with_fault_stop(
        self,
        envelope: ControlEventDeliveryEnvelope,
    ) -> None:
        put_task = asyncio.create_task(self._queue.put(envelope))
        fault_task = asyncio.create_task(self._faulted.wait())
        done, _ = await asyncio.wait(
            {put_task, fault_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if put_task in done and not put_task.cancelled():
            fault_task.cancel()
            with suppress(asyncio.CancelledError):
                await fault_task
            await put_task
            return
        if fault_task in done and self._lifecycle is GateLifecycle.FAULTED:
            if not put_task.done():
                put_task.cancel()
                with suppress(asyncio.CancelledError):
                    await put_task
            raise GateNotAcceptingError("Gate faulted while admission was waiting")

        fault_task.cancel()
        with suppress(asyncio.CancelledError):
            await fault_task
        await put_task

    async def _consume(self) -> None:
        while True:
            item = await self._queue.get()
            if item is _STOP:
                self._queue.task_done()
                return

            envelope = cast(ControlEventDeliveryEnvelope, item)
            self._in_flight = envelope
            try:
                await self._consumer.handle_control_turn(envelope)
            except asyncio.CancelledError:
                self._queue.task_done()
                self._in_flight = None
                raise
            except Exception as exc:
                self._failed_envelope = envelope
                self._queue.task_done()
                self._in_flight = None
                self._enter_fault(exc)
                return
            else:
                self._queue.task_done()
                self._in_flight = None

            if self._lifecycle is GateLifecycle.FAULTED:
                return

    def _require_accepting(self) -> None:
        if self._lifecycle is not GateLifecycle.RUNNING:
            raise GateNotAcceptingError(
                f"Gate is not accepting while {self._lifecycle.value}"
            )

    def _validate_scope(self, envelope: ControlEventDeliveryEnvelope) -> None:
        if (
            envelope.event.game_id != self._game_id
            or envelope.event.session_id != self._session_id
        ):
            raise GateScopeError("Envelope scope does not match Gate scope")

    def _enter_fault(self, error: BaseException) -> None:
        if self._lifecycle is GateLifecycle.FAULTED:
            return
        self._fault = error
        self._lifecycle = GateLifecycle.FAULTED
        self._faulted.set()
        consumer_task = self._consumer_task
        if (
            consumer_task is not None
            and self._in_flight is None
            and not consumer_task.done()
        ):
            consumer_task.cancel()

    def _require_consumer_task(self) -> asyncio.Task[None]:
        if self._consumer_task is None:
            raise GateLifecycleError("Gate has no consumer task")
        return self._consumer_task


def _delivery_identity(
    envelope: ControlEventDeliveryEnvelope,
) -> DeliveryIdentity:
    return (
        envelope.event_sequence_no,
        envelope.event.event_id,
        envelope.operation_id,
        envelope.command_id,
    )


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")


def _require_positive_int(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
