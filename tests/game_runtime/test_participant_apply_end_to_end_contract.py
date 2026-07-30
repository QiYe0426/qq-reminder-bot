from __future__ import annotations

import ast
import asyncio
from collections import deque
from dataclasses import replace
import inspect

import pytest

from game_runtime.actor.async_gate import GateAdmission, GateLifecycle, SessionAsyncGate
from game_runtime.actor.session_actor import GameSessionActor
from game_runtime.event import (
    CharacterAssignedPayload,
    GameEventType,
    PlayerReplacedPayload,
    SessionControlRejectedPayload,
    validate_control_result_event,
)
from game_runtime.participant import ParticipantMembershipState
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control import (
    ActorOwnedApplyCoordinator,
    ActorOwnedGameStateCompletionBoundary,
    ActorValidatedControlTurnEvidence,
    ActorVisibleGameState,
    CharacterAvailabilityStatus,
    CompositeGameControlApplyPlanBuilder,
    ControlCharacterAssignmentEvidence,
    ControlClaimAttemptEvidence,
    ControlCompletionKind,
    ControlEventDeliveryEnvelope,
    ControlParticipantBuildView,
    ControlSetupParticipantApplyEvidence,
    ControlTurnFailureReason,
    ControlTurnProcessingError,
    ControlTurnProcessor,
    OwnershipIntentType,
    ParticipantSnapshotRecord,
    ParticipantSnapshotSlice,
    SessionCommandType,
    SetupParticipantEvidenceStatus,
)
from game_runtime.session_control.apply_contract import ControlApplyPlan, ControlRejectPlan
from game_runtime.session_control.event_integration import DMCommandEventPayload
from test_actor_control_turn_integration_contract import make_session
from test_participant_control_plane_contract import _context as participant_context
from test_phase_apply_end_to_end_contract import (
    StrictRecordingAtomicApplyPort,
    _owned_async_queues,
)


def _replace_participant(
    context,
    participant_id: str,
    *,
    character_id: str | None,
):
    participant_views = tuple(
        replace(view, character_id=character_id)
        if view.participant_id == participant_id
        else view
        for view in context.participant_views
    )
    current = context.session_view.current_game_snapshot
    assert current is not None
    records = tuple(
        ParticipantSnapshotRecord(
            view.participant_id,
            view.participant_type,
            view.membership_state,
            view.character_id,
            view.binding_version,
        )
        for view in sorted(participant_views, key=lambda item: item.participant_id)
    )
    return replace(
        context,
        participant_views=participant_views,
        session_view=replace(
            context.session_view,
            current_game_snapshot=replace(
                current,
                participants=ParticipantSnapshotSlice(
                    current.participants.schema_version,
                    current.participants.domain_version,
                    records,
                ),
            ),
        ),
    )


def _already_assigned_context():
    return _replace_participant(
        participant_context(
            availability=CharacterAvailabilityStatus.ASSIGNED_TO_TARGET
        ),
        "player-old",
        character_id="character-1",
    )


def _conflict_context():
    return _replace_participant(
        participant_context(
            availability=CharacterAvailabilityStatus.ASSIGNED_TO_OTHER
        ),
        "player-new",
        character_id="character-1",
    )


def _invalid_assignment_lifecycle_context():
    return participant_context(
        status=GameSessionStatus.RUNNING,
        phase=GamePhase.INTRODUCTION,
    )


def _replacement_not_allowed_context():
    return participant_context(
        SessionCommandType.REPLACE_PLAYER,
        status=GameSessionStatus.RUNNING,
        phase=GamePhase.INTRODUCTION,
    )


def _invalid_replacement_context():
    return _replace_participant(
        participant_context(SessionCommandType.REPLACE_PLAYER),
        "player-new",
        character_id="character-2",
    )


def _envelope(context, *, sequence: int | None = None) -> ControlEventDeliveryEnvelope:
    original = context.envelope
    if sequence is None or sequence == original.event_sequence_no:
        return original
    command_id = f"command-{sequence}"
    operation_id = f"operation-{sequence}"
    event_id = f"event-{sequence}"
    causation_id = f"request-{sequence}"
    correlation_id = f"correlation-{sequence}"
    payload = DMCommandEventPayload(
        command_id=command_id,
        command_type=context.command_intent.command_type,
        requester=original.requester_principal_ref,
        causation_event_id=causation_id,
        observed_state_version=original.observed_state_version,
        payload_reference=f"sha256:{context.command_intent.payload_fingerprint}",
        payload_fingerprint=context.command_intent.payload_fingerprint,
    )
    event = replace(
        original.event,
        event_id=event_id,
        correlation_id=correlation_id,
        causation_event_id=causation_id,
        payload=payload.to_mapping(),
    )
    return replace(
        original,
        event=event,
        event_sequence_no=sequence,
        operation_id=operation_id,
        command_id=command_id,
        correlation_id=correlation_id,
        stored_event_reference=event_id,
    )


class StateBoundParticipantEvidenceFactory:
    """Evidence-only adapter binding participant command facts to Actor truth."""

    __slots__ = ("build_calls", "evidence", "turns")

    def __init__(self, turns) -> None:
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
        context_factory, mode = self.turns[envelope.event.event_id]
        context = context_factory()
        snapshot = state.snapshot
        participant_views = tuple(
            ControlParticipantBuildView(
                game_id=snapshot.game_id,
                session_id=snapshot.session_id,
                participant_id=record.participant_id,
                participant_type=record.participant_type,
                membership_state=record.membership_state,
                character_id=record.character_id,
                binding_version=record.binding_version,
            )
            for record in snapshot.participants.participants
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
        participant_evidence = context.setup_participant_evidence
        assignment = participant_evidence.character_assignment
        replacement = participant_evidence.player_replacement
        if assignment is not None:
            availability = replace(
                assignment.availability_evidence,
                observed_state_version=snapshot.state_version,
            )
            assignment = replace(
                assignment,
                observed_state_version=snapshot.state_version,
                ownership_generation=state.ownership_generation,
                availability_evidence=availability,
            )
            if mode == "evidence":
                assignment = replace(
                    assignment,
                    validation_status=SetupParticipantEvidenceStatus.FAILED,
                )
            elif mode == "availability":
                assignment = replace(
                    assignment,
                    availability_evidence=replace(
                        availability,
                        availability_status=CharacterAvailabilityStatus.UNKNOWN,
                        occupying_participant_reference=None,
                    ),
                )
            elif mode == "ownership":
                assignment = replace(assignment, ownership_generation=1)
            elif mode == "binding":
                assignment = replace(
                    assignment,
                    expected_binding_version=3,
                    resulting_binding_version=4,
                    assignment_version=4,
                )
        if replacement is not None:
            replacement = replace(
                replacement,
                observed_state_version=snapshot.state_version,
                ownership_generation=state.ownership_generation,
            )
        participant_evidence = ControlSetupParticipantApplyEvidence(
            character_assignment=assignment,
            player_replacement=replacement,
        )
        evidence = ActorValidatedControlTurnEvidence(
            envelope=envelope,
            command_intent=context.command_intent,
            session_view=session_view,
            participant_views=participant_views,
            setup_view=context.setup_view,
            ownership_evidence=ownership,
            governance_schema_version=1,
            authorization_reference=envelope.authorization_reference,
            confirmation_reference=envelope.confirmation_reference,
            lifecycle_evidence=context.lifecycle_evidence,
            setup_participant_evidence=participant_evidence,
            claim_attempt=ControlClaimAttemptEvidence.from_envelope(
                envelope,
                claimed_at=context.claim.claimed_at,
            ),
        )
        self.evidence.append(evidence)
        return evidence


def _runtime(
    *,
    context_factories,
    modes: tuple[str | None, ...] | None = None,
    sequences: tuple[int, ...] | None = None,
    failure_mode: str | None = None,
    receipt_corruption: str | None = None,
    block_first_claim: bool = False,
):
    contexts = tuple(factory() for factory in context_factories)
    sequences = sequences or tuple(
        context.envelope.event_sequence_no for context in contexts
    )
    envelopes = tuple(
        _envelope(context, sequence=sequence)
        for context, sequence in zip(contexts, sequences)
    )
    first_snapshot = contexts[0].session_view.current_game_snapshot
    assert first_snapshot is not None
    initial = ActorVisibleGameState(
        snapshot=first_snapshot,
        committed_control_cursor=contexts[0].session_view.last_applied_sequence_no,
        ownership_generation=contexts[0].ownership_evidence.active_generation,
        last_completion_identity=None,
    )
    port = StrictRecordingAtomicApplyPort(
        ownership_generation=initial.ownership_generation,
        failure_mode=failure_mode,
        receipt_corruption=receipt_corruption,
        block_first_claim=block_first_claim,
    )
    dispatcher = CompositeGameControlApplyPlanBuilder()
    coordinator = ActorOwnedApplyCoordinator(apply_port=port, builder=dispatcher)
    processor = ControlTurnProcessor(coordinator)
    modes = modes or (None,) * len(envelopes)
    factory = StateBoundParticipantEvidenceFactory(
        {
            envelope.event.event_id: (context_factory, mode)
            for envelope, context_factory, mode in zip(
                envelopes, context_factories, modes
            )
        }
    )
    projection_seed = make_session()
    projection_seed = replace(
        projection_seed,
        dm_identity=replace(
            projection_seed.dm_identity,
            participant_id=initial.snapshot.dm_participant_id,
        ),
    )
    actor = GameSessionActor.for_composite_control(
        session_projection_seed=projection_seed,
        initial_state=initial,
        control_turn_evidence_factory=factory,
        control_turn_processor=processor,
    )
    gate = SessionAsyncGate(
        game_id=initial.snapshot.game_id,
        session_id=initial.snapshot.session_id,
        next_expected_sequence_no=envelopes[0].event_sequence_no,
        consumer=actor,
        max_queue_size=max(2, len(envelopes)),
    )
    return (
        contexts,
        initial,
        envelopes,
        port,
        dispatcher,
        coordinator,
        processor,
        factory,
        actor,
        gate,
    )


async def _drive(gate: SessionAsyncGate, envelopes) -> None:
    await gate.start()
    for envelope in envelopes:
        assert await gate.admit(envelope) is GateAdmission.ACCEPTED
    await gate.close()


@pytest.mark.parametrize(
    "status",
    (GameSessionStatus.CREATED, GameSessionStatus.PAUSED),
)
def test_assign_character_commits_one_actor_visible_participant_change(status) -> None:
    context_factory = lambda: participant_context(status=status)
    (
        _,
        initial,
        envelopes,
        port,
        dispatcher,
        coordinator,
        processor,
        factory,
        actor,
        gate,
    ) = _runtime(context_factories=(context_factory,))
    before = initial.snapshot

    asyncio.run(_drive(gate, envelopes))

    state = actor.visible_game_state
    plan = port.calls[1][1]
    assert gate.lifecycle is GateLifecycle.CLOSED
    assert [call[0] for call in port.calls] == [
        "claim_operation",
        "commit_control_apply",
    ]
    assert isinstance(plan, ControlApplyPlan)
    assert processor._coordinator is coordinator
    assert coordinator._builder is dispatcher
    assert gate._consumer is actor
    assert isinstance(
        actor._game_state_completion_boundary,
        ActorOwnedGameStateCompletionBoundary,
    )
    assert _owned_async_queues(
        port, dispatcher, coordinator, processor, factory, actor, gate
    ) == (gate._queue,)
    assert isinstance(actor._mailbox, deque)
    assert tuple(actor._mailbox) == ()
    assert actor.processed_event_ids == ()
    assert factory.build_calls == [(initial, envelopes[0])]

    candidate = state.snapshot
    assert candidate is plan.candidate_snapshot
    assert candidate.state_version == before.state_version + 1
    assert candidate.participants.domain_version == (
        before.participants.domain_version + 1
    )
    assert candidate.last_applied_sequence_no == envelopes[0].event_sequence_no
    assert state.committed_control_cursor == envelopes[0].event_sequence_no
    assert candidate.lifecycle is before.lifecycle
    assert candidate.phase is before.phase
    assert candidate.setup is before.setup
    assert candidate.game_rules is before.game_rules
    assert candidate.hidden_state is before.hidden_state
    before_records = {
        record.participant_id: record for record in before.participants.participants
    }
    after_records = {
        record.participant_id: record
        for record in candidate.participants.participants
    }
    target = after_records["player-old"]
    assert target.character_id == "character-1"
    assert target.binding_version == before_records["player-old"].binding_version + 1
    assert after_records["dm-1"] is before_records["dm-1"]
    assert after_records["player-new"] is before_records["player-new"]
    expected_ownership = (
        OwnershipIntentType.UNCHANGED
        if status is GameSessionStatus.CREATED
        else OwnershipIntentType.RETAIN
    )
    assert plan.ownership_intent.intent_type is expected_ownership
    if status is GameSessionStatus.CREATED:
        assert plan.ownership_intent.expected_generation is None
        assert plan.ownership_intent.resulting_generation is None
    else:
        assert plan.ownership_intent.expected_generation == 3
        assert plan.ownership_intent.resulting_generation == 3
    assert tuple(event.event_type for event in plan.result_events) == (
        GameEventType.CHARACTER_ASSIGNED,
    )
    payload = validate_control_result_event(plan.result_events[0])
    assert isinstance(payload, CharacterAssignedPayload)
    assert state.last_completion_identity is not None
    assert state.last_completion_identity.completion_kind is ControlCompletionKind.APPLIED


@pytest.mark.parametrize(
    ("status", "old_character"),
    [
        (GameSessionStatus.CREATED, "character-1"),
        (GameSessionStatus.PAUSED, "character-1"),
        (GameSessionStatus.CREATED, None),
        (GameSessionStatus.PAUSED, None),
    ],
)
def test_replace_player_commits_bilateral_change_in_one_apply(
    status,
    old_character,
) -> None:
    context_factory = lambda: participant_context(
        SessionCommandType.REPLACE_PLAYER,
        status=status,
        old_character=old_character,
    )
    (
        _,
        initial,
        envelopes,
        port,
        _,
        _,
        _,
        _,
        actor,
        gate,
    ) = _runtime(context_factories=(context_factory,))
    before = initial.snapshot

    asyncio.run(_drive(gate, envelopes))

    plan = port.calls[1][1]
    state = actor.visible_game_state
    assert isinstance(plan, ControlApplyPlan)
    assert state.snapshot.state_version == before.state_version + 1
    assert state.snapshot.participants.domain_version == (
        before.participants.domain_version + 1
    )
    before_records = {
        record.participant_id: record for record in before.participants.participants
    }
    after_records = {
        record.participant_id: record
        for record in state.snapshot.participants.participants
    }
    assert after_records["player-old"].membership_state is ParticipantMembershipState.REPLACED
    assert after_records["player-old"].character_id is None
    assert after_records["player-old"].binding_version == (
        before_records["player-old"].binding_version + 1
    )
    assert after_records["player-new"].membership_state is ParticipantMembershipState.ACTIVE
    assert after_records["player-new"].character_id == old_character
    assert after_records["player-new"].binding_version == (
        before_records["player-new"].binding_version + 1
    )
    assert after_records["dm-1"] is before_records["dm-1"]
    assert tuple(event.event_type for event in plan.result_events) == (
        GameEventType.PLAYER_REPLACED,
    )
    assert isinstance(
        validate_control_result_event(plan.result_events[0]),
        PlayerReplacedPayload,
    )


@pytest.mark.parametrize(
    ("context_factory", "reason"),
    [
        (_already_assigned_context, "CHARACTER_ALREADY_ASSIGNED"),
        (_conflict_context, "CHARACTER_CONFLICT"),
        (_invalid_assignment_lifecycle_context, "INVALID_LIFECYCLE"),
        (_replacement_not_allowed_context, "PLAYER_REPLACEMENT_NOT_ALLOWED"),
        (_invalid_replacement_context, "INVALID_REPLACEMENT"),
    ],
)
def test_participant_business_reject_commits_cursor_without_snapshot_visibility(
    context_factory,
    reason,
) -> None:
    (
        _,
        initial,
        envelopes,
        port,
        _,
        _,
        _,
        _,
        actor,
        gate,
    ) = _runtime(context_factories=(context_factory,))

    asyncio.run(_drive(gate, envelopes))

    plan = port.calls[1][1]
    state = actor.visible_game_state
    assert gate.lifecycle is GateLifecycle.CLOSED
    assert [call[0] for call in port.calls] == [
        "claim_operation",
        "commit_control_rejection",
    ]
    assert isinstance(plan, ControlRejectPlan)
    payload = validate_control_result_event(plan.rejection_event)
    assert isinstance(payload, SessionControlRejectedPayload)
    assert payload.reason_code == reason
    assert state.snapshot is initial.snapshot
    assert state.committed_control_cursor == envelopes[0].event_sequence_no
    assert state.last_completion_identity is not None
    assert state.last_completion_identity.completion_kind is ControlCompletionKind.REJECTED


def test_success_after_reject_allows_snapshot_cursor_to_lag_control_cursor() -> None:
    replace_factory = lambda: participant_context(SessionCommandType.REPLACE_PLAYER)
    (
        _,
        initial,
        envelopes,
        port,
        _,
        _,
        _,
        factory,
        actor,
        gate,
    ) = _runtime(
        context_factories=(_already_assigned_context, replace_factory),
        sequences=(7, 8),
    )

    asyncio.run(_drive(gate, envelopes))

    assert [call[0] for call in port.calls] == [
        "claim_operation",
        "commit_control_rejection",
        "claim_operation",
        "commit_control_apply",
    ]
    state_after_reject = factory.build_calls[1][0]
    assert state_after_reject.snapshot is initial.snapshot
    assert state_after_reject.snapshot.last_applied_sequence_no == 6
    assert state_after_reject.committed_control_cursor == 7
    assert actor.visible_game_state.snapshot.last_applied_sequence_no == 8
    assert actor.visible_game_state.committed_control_cursor == 8
    assert actor.visible_game_state.snapshot.state_version == 5


@pytest.mark.parametrize(
    ("mode", "failure_mode", "expected_reason", "expected_calls"),
    [
        (
            "evidence",
            None,
            ControlTurnFailureReason.BUILD_NON_COMMIT,
            ["claim_operation"],
        ),
        (
            "availability",
            None,
            ControlTurnFailureReason.BUILD_NON_COMMIT,
            ["claim_operation"],
        ),
        (
            "ownership",
            None,
            ControlTurnFailureReason.RECOVERY_REQUIRED,
            ["claim_operation"],
        ),
        (
            "binding",
            None,
            ControlTurnFailureReason.RECOVERY_REQUIRED,
            ["claim_operation"],
        ),
        (
            None,
            "invalid_receipt",
            ControlTurnFailureReason.RECEIPT_INVALID,
            ["claim_operation", "commit_control_apply"],
        ),
        (
            None,
            "port_failure",
            ControlTurnFailureReason.APPLY_UNKNOWN,
            ["claim_operation", "commit_control_apply"],
        ),
    ],
)
def test_fail_closed_inputs_fault_stop_before_participant_visibility(
    mode,
    failure_mode,
    expected_reason,
    expected_calls,
) -> None:
    (
        _,
        initial,
        envelopes,
        port,
        _,
        _,
        _,
        factory,
        actor,
        gate,
    ) = _runtime(
        context_factories=(participant_context,),
        modes=(mode,),
        failure_mode=failure_mode,
    )

    async def scenario() -> None:
        await gate.start()
        assert await gate.admit(envelopes[0]) is GateAdmission.ACCEPTED
        await gate.wait_faulted()

    asyncio.run(scenario())

    assert gate.lifecycle is GateLifecycle.FAULTED
    assert isinstance(gate.fault, ControlTurnProcessingError)
    assert gate.fault.reason is expected_reason
    assert gate.failed_envelope is envelopes[0]
    assert actor.visible_game_state is initial
    assert actor.visible_game_state.committed_control_cursor == 6
    assert factory.build_calls == [(initial, envelopes[0])]
    assert [call[0] for call in port.calls] == expected_calls


def test_duplicate_admission_fault_stop_keeps_one_queue_and_old_mailbox_isolated() -> None:
    (
        _,
        initial,
        envelopes,
        port,
        dispatcher,
        coordinator,
        processor,
        factory,
        actor,
        gate,
    ) = _runtime(
        context_factories=(participant_context,),
        failure_mode="port_failure",
        block_first_claim=True,
    )
    envelope = envelopes[0]

    async def scenario() -> None:
        await gate.start()
        first = asyncio.create_task(gate.admit(envelope))
        await port.first_claim_started.wait()
        assert await first is GateAdmission.ACCEPTED
        assert await gate.admit(envelope) is GateAdmission.DUPLICATE
        port.release_first_claim.set()
        await gate.wait_faulted()

    asyncio.run(scenario())

    assert gate.lifecycle is GateLifecycle.FAULTED
    assert isinstance(gate.fault, ControlTurnProcessingError)
    assert gate.fault.reason is ControlTurnFailureReason.APPLY_UNKNOWN
    assert actor.visible_game_state is initial
    assert _owned_async_queues(
        port, dispatcher, coordinator, processor, factory, actor, gate
    ) == (gate._queue,)
    assert isinstance(actor._mailbox, deque)
    assert tuple(actor._mailbox) == ()
    assert actor.processed_event_ids == ()
    assert [item for _, item in factory.build_calls] == [envelope]


def test_participant_e2e_adapters_have_no_forbidden_runtime_dependencies() -> None:
    module = inspect.getmodule(_runtime)
    assert module is not None
    tree = ast.parse(inspect.getsource(module))
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
    forbidden = {
        "persistence",
        "event_store",
        "eventstore",
        "recovery",
        "notification",
        "database",
        "sqlite",
        "plugin",
        "tool",
        "llm",
    }
    assert not any(
        token in imported.casefold()
        for imported in imports
        for token in forbidden
    )
    factory_calls = {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(
            ast.parse(inspect.getsource(StateBoundParticipantEvidenceFactory))
        )
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
        "recover",
        "open",
        "connect",
        "execute",
    }.isdisjoint(factory_calls)
    runtime_calls = [
        node.func.id
        for node in ast.walk(ast.parse(inspect.getsource(_runtime)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert runtime_calls.count("CompositeGameControlApplyPlanBuilder") == 1
    assert runtime_calls.count("ActorOwnedApplyCoordinator") == 1
    assert runtime_calls.count("ControlTurnProcessor") == 1
    assert runtime_calls.count("SessionAsyncGate") == 1
