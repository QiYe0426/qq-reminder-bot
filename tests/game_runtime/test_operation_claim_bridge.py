from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from importlib import import_module

import pytest

from game_runtime.event import EventVisibility, GameEvent, GameEventSource, GameEventType
from game_runtime.session_control import (
    ControlApplyConflict,
    ControlApplyConflictReason,
    ControlApplyStorageFailure,
    ControlEventDeliveryEnvelope,
    ControlOperationClaim,
    DMCommandEventPayload,
    SessionCommandType,
)


NOW = datetime(2026, 7, 16, 14, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def bridge_module():
    return import_module("game_runtime.session_control.claim_bridge")


def make_envelope() -> ControlEventDeliveryEnvelope:
    payload = DMCommandEventPayload(
        command_id="command-1",
        command_type=SessionCommandType.PAUSE_GAME,
        requester="principal-dm-1",
        causation_event_id="message-1",
        observed_state_version=4,
        payload_reference="sha256:" + "a" * 64,
        payload_fingerprint="a" * 64,
    )
    event = GameEvent(
        event_id="event-command-1",
        game_id="game-1",
        session_id="session-1",
        event_type=GameEventType.DM_COMMAND,
        actor="principal-dm-1",
        source=GameEventSource.CONTROL,
        correlation_id="correlation-1",
        timestamp=NOW,
        payload=payload.to_mapping(),
        visibility=EventVisibility.DM_CONTROL,
        observed_state_version=4,
        causation_event_id="message-1",
    )
    return ControlEventDeliveryEnvelope(
        event=event,
        event_sequence_no=7,
        operation_id="operation-1",
        command_id="command-1",
        observed_state_version=4,
        requester_principal_ref="principal-dm-1",
        requester_binding_version=2,
        authorization_reference="authorization-1",
        confirmation_reference="confirmation-1",
        correlation_id="correlation-1",
        stored_event_reference="event-command-1",
    )


def make_claim(
    *,
    claim_id: str = "claim-1",
    claimed_at: datetime = NOW,
) -> ControlOperationClaim:
    return ControlOperationClaim(
        game_id="game-1",
        session_id="session-1",
        command_id="command-1",
        operation_id="operation-1",
        input_event_id="event-command-1",
        claim_id=claim_id,
        claimed_at=claimed_at,
    )


class FakeSessionControlApplyPort:
    def __init__(self, responder) -> None:
        self._responder = responder
        self.claim_calls: list[dict[str, object]] = []
        self.forbidden_calls = 0

    async def claim_operation(self, **kwargs) -> ControlOperationClaim:
        self.claim_calls.append(dict(kwargs))
        if isinstance(self._responder, BaseException):
            raise self._responder
        if callable(self._responder):
            return self._responder(kwargs)
        return self._responder

    async def commit_control_apply(self, *args, **kwargs):
        self.forbidden_calls += 1
        raise AssertionError("Bridge must not execute Apply")

    async def commit_control_rejection(self, *args, **kwargs):
        self.forbidden_calls += 1
        raise AssertionError("Bridge must not commit rejection")

    async def create_session_with_event(self, *args, **kwargs):
        self.forbidden_calls += 1
        raise AssertionError("Bridge must not execute bootstrap")


def acquire(bridge, *, claim_id: str = "claim-1", claimed_at: datetime = NOW):
    return bridge.acquire(make_envelope(), claim_id, claimed_at)


def test_acquire_claims_from_envelope_evidence_and_returns_immutable_claim() -> None:
    expected = make_claim()
    port = FakeSessionControlApplyPort(expected)
    bridge = bridge_module().OperationClaimBridge(port)

    result = run(acquire(bridge))

    assert result is expected
    assert port.claim_calls == [
        {
            "game_id": "game-1",
            "session_id": "session-1",
            "command_id": "command-1",
            "operation_id": "operation-1",
            "input_event_id": "event-command-1",
            "claim_id": "claim-1",
            "claimed_at": NOW,
        }
    ]
    assert port.forbidden_calls == 0


def test_same_claim_id_is_idempotently_returned_by_the_port() -> None:
    expected = make_claim()
    port = FakeSessionControlApplyPort(expected)
    bridge = bridge_module().OperationClaimBridge(port)

    first = run(acquire(bridge))
    second = run(acquire(bridge))

    assert first is expected
    assert second is expected
    assert len(port.claim_calls) == 2
    assert {call["claim_id"] for call in port.claim_calls} == {"claim-1"}


def test_different_claim_id_conflict_is_typed_and_not_retried() -> None:
    def responder(kwargs):
        if len(port.claim_calls) == 1:
            return make_claim(claim_id=str(kwargs["claim_id"]))
        raise ControlApplyConflict(
            ControlApplyConflictReason.OPERATION_CLAIM_MISMATCH,
            expected="claim-1",
            actual=kwargs["claim_id"],
        )

    port = FakeSessionControlApplyPort(responder)
    bridge = bridge_module().OperationClaimBridge(port)
    run(acquire(bridge))

    with pytest.raises(bridge_module().OperationClaimConflict) as captured:
        run(acquire(bridge, claim_id="claim-2", claimed_at=NOW + timedelta(seconds=1)))

    assert captured.value.reason is ControlApplyConflictReason.OPERATION_CLAIM_MISMATCH
    assert len(port.claim_calls) == 2
    assert port.forbidden_calls == 0


def test_terminal_operation_conflict_remains_deterministic() -> None:
    conflict = ControlApplyConflict(
        ControlApplyConflictReason.IDEMPOTENCY_CONFLICT,
        expected="CREATED",
        actual="SUCCESS",
    )
    port = FakeSessionControlApplyPort(conflict)
    bridge = bridge_module().OperationClaimBridge(port)

    with pytest.raises(bridge_module().OperationClaimConflict) as captured:
        run(acquire(bridge))

    assert captured.value.reason is ControlApplyConflictReason.IDEMPOTENCY_CONFLICT
    assert captured.value.actual == "SUCCESS"
    assert len(port.claim_calls) == 1


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        (
            ControlApplyStorageFailure("storage unavailable"),
            "STORAGE_FAILURE",
        ),
        (TimeoutError("claim timed out"), "TIMEOUT"),
        (asyncio.CancelledError(), "CANCELLED"),
    ],
)
def test_indeterminate_port_failures_are_unknown_without_retry(
    failure: BaseException,
    expected_reason: str,
) -> None:
    port = FakeSessionControlApplyPort(failure)
    bridge = bridge_module().OperationClaimBridge(port)

    with pytest.raises(bridge_module().OperationClaimUnknown) as captured:
        run(acquire(bridge))

    assert captured.value.reason.value == expected_reason
    assert len(port.claim_calls) == 1
    assert port.forbidden_calls == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("game_id", "game-other"),
        ("session_id", "session-other"),
        ("command_id", "command-other"),
        ("operation_id", "operation-other"),
        ("input_event_id", "event-other"),
        ("claim_id", "claim-other"),
        ("claimed_at", NOW + timedelta(seconds=1)),
    ],
)
def test_returned_claim_evidence_mismatch_is_unknown(
    field: str,
    value: object,
) -> None:
    mismatched = replace(make_claim(), **{field: value})
    port = FakeSessionControlApplyPort(mismatched)
    bridge = bridge_module().OperationClaimBridge(port)

    with pytest.raises(bridge_module().OperationClaimUnknown) as captured:
        run(acquire(bridge))

    assert captured.value.reason is (
        bridge_module().OperationClaimUnknownReason.CLAIM_EVIDENCE_MISMATCH
    )
    assert len(port.claim_calls) == 1
    assert port.forbidden_calls == 0


def test_unexpected_port_failure_is_unknown_and_never_enters_followup_flow() -> None:
    port = FakeSessionControlApplyPort(RuntimeError("adapter failure"))
    bridge = bridge_module().OperationClaimBridge(port)

    with pytest.raises(bridge_module().OperationClaimUnknown) as captured:
        run(acquire(bridge))

    assert captured.value.reason is (
        bridge_module().OperationClaimUnknownReason.UNEXPECTED_FAILURE
    )
    assert len(port.claim_calls) == 1
    assert port.forbidden_calls == 0


def test_claim_bridge_contract_is_exported_from_session_control_package() -> None:
    package = import_module("game_runtime.session_control")

    assert package.OperationClaimBridge is bridge_module().OperationClaimBridge
    assert package.OperationClaimConflict is bridge_module().OperationClaimConflict
    assert package.OperationClaimUnknown is bridge_module().OperationClaimUnknown
