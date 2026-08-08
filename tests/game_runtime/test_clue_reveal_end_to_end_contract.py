from __future__ import annotations

import asyncio
from collections import deque

import pytest

from game_runtime.actor.async_gate import GateAdmission, GateLifecycle
from game_runtime.event import (
    ClueRevealedPayload,
    GameEventType,
    SessionControlRejectedPayload,
    validate_control_result_event,
)
from game_runtime.session_control import (
    ActorOwnedGameStateCompletionBoundary,
    ControlApplyPlan,
    ControlCompletionKind,
    ControlRejectPlan,
    ControlTurnFailureReason,
    ControlTurnProcessingError,
)
from test_clue_reveal_control_plane_contract import _reveal_context
from test_game_rule_activation_end_to_end_contract import (
    _drive,
    _owned_async_queues,
    _runtime,
)


def test_public_clue_reveal_commits_before_actor_visibility() -> None:
    (
        _,
        initial,
        envelope,
        port,
        dispatcher,
        coordinator,
        processor,
        factory,
        actor,
        gate,
    ) = _runtime(context_factory=_reveal_context)
    before = initial.snapshot

    asyncio.run(_drive(gate, envelope))

    state = actor.visible_game_state
    plan = port.calls[1][1]
    assert gate.lifecycle is GateLifecycle.CLOSED
    assert [call[0] for call in port.calls] == [
        "claim_operation",
        "commit_control_apply",
    ]
    assert isinstance(plan, ControlApplyPlan)
    assert port.visible_states_at_commit == [initial]
    assert state.snapshot is plan.candidate_snapshot
    assert state.snapshot is not before
    assert state.snapshot.state_version == before.state_version + 1
    assert state.snapshot.last_applied_sequence_no == envelope.event_sequence_no
    assert state.committed_control_cursor == envelope.event_sequence_no
    assert state.snapshot.lifecycle is before.lifecycle
    assert state.snapshot.phase is before.phase
    assert state.snapshot.setup is before.setup
    assert state.snapshot.participants is before.participants
    assert state.snapshot.game_rules.domain_version == (
        before.game_rules.domain_version + 1
    )
    assert state.snapshot.hidden_state.domain_version == (
        before.hidden_state.domain_version + 1
    )
    assert (
        state.snapshot.game_rules.committed_disclosure_state_reference
        == "disclosure-state:commit-2"
    )
    assert (
        state.snapshot.hidden_state.committed_state_reference
        == "hidden-state:commit-2"
    )
    assert state.last_completion_identity is not None
    assert (
        state.last_completion_identity.completion_kind
        is ControlCompletionKind.APPLIED
    )
    assert isinstance(
        actor._game_state_completion_boundary,
        ActorOwnedGameStateCompletionBoundary,
    )
    assert coordinator._builder is dispatcher
    assert processor._coordinator is coordinator
    assert gate._consumer is actor
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

    assert tuple(event.event_type for event in plan.result_events) == (
        GameEventType.CLUE_REVEALED,
    )
    payload = validate_control_result_event(plan.result_events[0])
    assert isinstance(payload, ClueRevealedPayload)
    assert payload.clue_id == "clue-1"
    assert payload.public_disclosure_reference == "public-disclosure:clue-1"
    assert "hidden-state" not in repr(payload)
    assert "provenance" not in repr(payload)


@pytest.mark.parametrize(
    ("disposition", "reason"),
    [
        ("RULE_SET_NOT_ACTIVE", "RULE_SET_NOT_ACTIVE"),
        ("ALREADY_REVEALED", "CLUE_ALREADY_REVEALED"),
        ("NOT_FOUND", "CLUE_NOT_FOUND"),
        ("NOT_REVEALABLE", "CLUE_NOT_REVEALABLE"),
    ],
)
def test_business_reject_commits_control_cursor_without_snapshot_swap(
    disposition: str,
    reason: str,
) -> None:
    context_factory = lambda: _reveal_context(disposition_name=disposition)
    runtime = _runtime(context_factory=context_factory)
    initial, envelope, port, actor, gate = (
        runtime[1],
        runtime[2],
        runtime[3],
        runtime[8],
        runtime[9],
    )

    asyncio.run(_drive(gate, envelope))

    plan = port.calls[1][1]
    state = actor.visible_game_state
    assert isinstance(plan, ControlRejectPlan)
    assert [call[0] for call in port.calls] == [
        "claim_operation",
        "commit_control_rejection",
    ]
    payload = validate_control_result_event(plan.rejection_event)
    assert isinstance(payload, SessionControlRejectedPayload)
    assert payload.reason_code == reason
    assert state.snapshot is initial.snapshot
    assert state.committed_control_cursor == envelope.event_sequence_no
    assert state.last_completion_identity is not None
    assert (
        state.last_completion_identity.completion_kind
        is ControlCompletionKind.REJECTED
    )


@pytest.mark.parametrize(
    ("failure_mode", "expected_reason"),
    [
        ("port_failure", ControlTurnFailureReason.APPLY_UNKNOWN),
        ("invalid_receipt", ControlTurnFailureReason.RECEIPT_INVALID),
    ],
)
def test_unknown_commit_outcome_fault_stops_without_speculative_visibility(
    failure_mode: str,
    expected_reason: ControlTurnFailureReason,
) -> None:
    runtime = _runtime(
        context_factory=_reveal_context,
        failure_mode=failure_mode,
    )
    initial, envelope, port, actor, gate = (
        runtime[1],
        runtime[2],
        runtime[3],
        runtime[8],
        runtime[9],
    )

    async def scenario() -> None:
        await gate.start()
        assert await gate.admit(envelope) is GateAdmission.ACCEPTED
        await gate.wait_faulted()

    asyncio.run(scenario())

    assert gate.lifecycle is GateLifecycle.FAULTED
    assert isinstance(gate.fault, ControlTurnProcessingError)
    assert gate.fault.reason is expected_reason
    assert actor.visible_game_state is initial
    assert [call[0] for call in port.calls] == [
        "claim_operation",
        "commit_control_apply",
    ]


def test_exact_duplicate_reveal_is_admitted_once_and_committed_once() -> None:
    runtime = _runtime(
        context_factory=_reveal_context,
        block_first_claim=True,
    )
    envelope, port, factory, actor, gate = (
        runtime[2],
        runtime[3],
        runtime[7],
        runtime[8],
        runtime[9],
    )

    async def scenario() -> None:
        await gate.start()
        first = asyncio.create_task(gate.admit(envelope))
        await port.first_claim_started.wait()
        assert await first is GateAdmission.ACCEPTED
        assert await gate.admit(envelope) is GateAdmission.DUPLICATE
        port.release_first_claim.set()
        await gate.close()

    asyncio.run(scenario())

    assert gate.lifecycle is GateLifecycle.CLOSED
    assert [call[0] for call in port.calls] == [
        "claim_operation",
        "commit_control_apply",
    ]
    assert len(factory.build_calls) == 1
    assert actor.visible_game_state.committed_control_cursor == (
        envelope.event_sequence_no
    )
