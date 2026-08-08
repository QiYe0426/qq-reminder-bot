from __future__ import annotations

import asyncio
from collections import deque

import pytest

from game_runtime.actor.async_gate import GateAdmission, GateLifecycle
from game_runtime.event import (
    GameEventType,
    QuestActivatedPayload,
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
    QuestActivationDisposition,
)
from test_game_rule_activation_end_to_end_contract import (
    _drive,
    _owned_async_queues,
    _runtime,
)
from test_quest_apply_composition_contract import _quest_context


def test_quest_activation_commits_before_actor_visibility() -> None:
    runtime = _runtime(context_factory=_quest_context)
    initial, envelope, port, dispatcher, coordinator, processor, factory, actor, gate = (
        runtime[1],
        runtime[2],
        runtime[3],
        runtime[4],
        runtime[5],
        runtime[6],
        runtime[7],
        runtime[8],
        runtime[9],
    )
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
    assert state.snapshot.quest.active_quest_id == "quest-1"
    assert state.snapshot.quest.domain_version == 1
    assert state.snapshot.hidden_state.domain_version == (
        before.hidden_state.domain_version + 1
    )
    assert state.snapshot.game_rules is before.game_rules
    assert state.snapshot.lifecycle is before.lifecycle
    assert state.snapshot.phase is before.phase
    assert state.snapshot.setup is before.setup
    assert state.snapshot.participants is before.participants
    assert state.committed_control_cursor == envelope.event_sequence_no
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
        GameEventType.QUEST_ACTIVATED,
    )
    payload = validate_control_result_event(plan.result_events[0])
    assert isinstance(payload, QuestActivatedPayload)
    assert payload.quest_id == "quest-1"
    assert payload.public_state_reference == "quest-public:commit-1"
    public_surfaces = (
        repr(payload),
        repr(port.receipts[0].result_event_references),
        repr(state.last_completion_identity),
    )
    for surface in public_surfaces:
        assert "hidden-state" not in surface
        assert "provenance" not in surface


@pytest.mark.parametrize(
    ("disposition", "reason"),
    [
        (QuestActivationDisposition.ALREADY_ACTIVE, "QUEST_ALREADY_ACTIVE"),
        (QuestActivationDisposition.CONFLICT, "QUEST_CONFLICT"),
        (QuestActivationDisposition.NOT_FOUND, "QUEST_NOT_FOUND"),
        (
            QuestActivationDisposition.NOT_ACTIVATABLE,
            "QUEST_NOT_ACTIVATABLE",
        ),
        (
            QuestActivationDisposition.RULE_SET_NOT_ACTIVE,
            "RULE_SET_NOT_ACTIVE",
        ),
    ],
)
def test_business_rejection_commits_cursor_without_snapshot_replacement(
    disposition: QuestActivationDisposition,
    reason: str,
) -> None:
    runtime = _runtime(
        context_factory=lambda: _quest_context(disposition=disposition)
    )
    initial, envelope, port, actor, gate = (
        runtime[1],
        runtime[2],
        runtime[3],
        runtime[8],
        runtime[9],
    )

    asyncio.run(_drive(gate, envelope))

    state = actor.visible_game_state
    plan = port.calls[1][1]
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
    ("failure_mode", "reason"),
    [
        ("port_failure", ControlTurnFailureReason.APPLY_UNKNOWN),
        ("invalid_receipt", ControlTurnFailureReason.RECEIPT_INVALID),
    ],
)
def test_commit_failure_fault_stops_without_speculative_visibility(
    failure_mode: str,
    reason: ControlTurnFailureReason,
) -> None:
    runtime = _runtime(
        context_factory=_quest_context,
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
    assert gate.fault.reason is reason
    assert actor.visible_game_state is initial
    assert [call[0] for call in port.calls] == [
        "claim_operation",
        "commit_control_apply",
    ]


def test_exact_duplicate_quest_operation_executes_once() -> None:
    runtime = _runtime(
        context_factory=_quest_context,
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
