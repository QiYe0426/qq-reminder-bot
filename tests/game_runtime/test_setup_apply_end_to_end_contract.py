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
    ScriptSetPayload,
    SessionControlRejectedPayload,
    validate_control_result_event,
)
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control import (
    ActorOwnedApplyCoordinator,
    ActorOwnedGameStateCompletionBoundary,
    ActorControlTurnValidationError,
    ActorValidatedControlTurnEvidence,
    ActorVisibleGameState,
    CompositeGameControlApplyPlanBuilder,
    ControlClaimAttemptEvidence,
    ControlCompletionKind,
    ControlEventDeliveryEnvelope,
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
from test_actor_control_turn_integration_contract import make_session
from test_phase_apply_end_to_end_contract import (
    StrictRecordingAtomicApplyPort,
    _owned_async_queues,
)
from test_setup_control_plane_contract import _context as setup_context


class StateBoundSetupEvidenceFactory:
    """Ingress adapter fake that binds SET_SCRIPT evidence to Actor truth."""

    __slots__ = ("build_calls", "context_factory", "evidence", "mode")

    def __init__(self, context_factory, *, mode: str | None = None) -> None:
        self.context_factory = context_factory
        self.mode = mode
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
        context = self.context_factory()
        snapshot = state.snapshot
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
        setup_evidence = context.setup_participant_evidence
        if self.mode == "evidence_scope":
            script = setup_evidence.script_apply
            assert script is not None
            setup_evidence = replace(
                setup_evidence,
                script_apply=replace(script, session_id="wrong-session"),
            )
        elif self.mode == "evidence_version":
            script = setup_evidence.script_apply
            assert script is not None
            setup_evidence = replace(
                setup_evidence,
                script_apply=replace(
                    script,
                    expected_setup_version=script.expected_setup_version + 1,
                    resulting_setup_version=script.resulting_setup_version + 1,
                ),
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
            setup_participant_evidence=setup_evidence,
            claim_attempt=ControlClaimAttemptEvidence.from_envelope(
                envelope,
                claimed_at=context.claim.claimed_at,
            ),
        )
        self.evidence.append(evidence)
        return evidence


def _initial_state(context) -> ActorVisibleGameState:
    snapshot = context.session_view.current_game_snapshot
    assert snapshot is not None
    return ActorVisibleGameState(
        snapshot=snapshot,
        committed_control_cursor=snapshot.last_applied_sequence_no,
        ownership_generation=context.ownership_evidence.active_generation,
        last_completion_identity=None,
    )


def _envelope(context) -> ControlEventDeliveryEnvelope:
    return context.envelope


def _runtime(
    *,
    context_factory,
    failure_mode: str | None = None,
    receipt_corruption: str | None = None,
    evidence_mode: str | None = None,
    block_first_claim: bool = False,
):
    context = context_factory()
    initial = _initial_state(context)
    envelope = _envelope(context)
    port = StrictRecordingAtomicApplyPort(
        ownership_generation=initial.ownership_generation,
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
    factory = StateBoundSetupEvidenceFactory(
        context_factory,
        mode=evidence_mode,
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
        next_expected_sequence_no=envelope.event_sequence_no,
        consumer=actor,
        max_queue_size=2,
    )
    return (
        context,
        initial,
        envelope,
        port,
        dispatcher,
        coordinator,
        processor,
        factory,
        actor,
        gate,
    )


async def _drive(
    gate: SessionAsyncGate,
    envelope: ControlEventDeliveryEnvelope,
) -> None:
    await gate.start()
    assert await gate.admit(envelope) is GateAdmission.ACCEPTED
    await gate.close()


def test_initialize_set_script_commits_through_single_real_control_plane() -> None:
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
    ) = _runtime(context_factory=setup_context)
    before = initial.snapshot

    asyncio.run(_drive(gate, envelope))

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
    assert factory.build_calls == [(initial, envelope)]
    assert factory.evidence[0].envelope is envelope
    assert factory.evidence[0].session_view.current_game_snapshot is before

    plan = port.calls[1][1]
    assert isinstance(plan, ControlApplyPlan)
    candidate = state.snapshot
    assert candidate is plan.candidate_snapshot
    assert candidate.setup.script_id == "script-2"
    assert candidate.setup.public_name == "Public Script 2"
    assert candidate.setup.manifest_reference == "manifest-2"
    assert candidate.setup.manifest_version == 3
    assert candidate.setup.domain_version == before.setup.domain_version + 1
    assert candidate.state_version == before.state_version + 1
    assert candidate.last_applied_sequence_no == envelope.event_sequence_no
    assert candidate.lifecycle is before.lifecycle
    assert candidate.phase is before.phase
    assert candidate.participants is before.participants
    assert candidate.game_rules is before.game_rules
    assert candidate.hidden_state is before.hidden_state
    assert (
        plan.ownership_intent.intent_type
        is OwnershipIntentType.UNCHANGED
    )
    assert plan.ownership_intent.expected_generation is None
    assert plan.ownership_intent.resulting_generation is None
    assert tuple(event.event_type for event in plan.result_events) == (
        GameEventType.SCRIPT_SET,
    )
    payload = validate_control_result_event(plan.result_events[0])
    assert isinstance(payload, ScriptSetPayload)
    assert payload.script_id == "script-2"
    assert state.committed_control_cursor == envelope.event_sequence_no
    assert state.ownership_generation is None
    assert state.last_completion_identity is not None
    assert (
        state.last_completion_identity.completion_kind
        is ControlCompletionKind.APPLIED
    )
    # The Actor enforces SnapshotVisibilityAccepted before returning; a closed
    # Gate plus the visible candidate proves that typed completion path.


def test_confirmed_replacement_commits_new_setup_slice() -> None:
    context_factory = lambda: setup_context(existing=True)
    (
        _,
        initial,
        envelope,
        port,
        _,
        _,
        _,
        _,
        actor,
        gate,
    ) = _runtime(context_factory=context_factory)
    before = initial.snapshot

    asyncio.run(_drive(gate, envelope))

    state = actor.visible_game_state
    plan = port.calls[1][1]
    assert isinstance(plan, ControlApplyPlan)
    assert state.snapshot.setup is not before.setup
    assert state.snapshot.setup.script_id == "script-2"
    assert state.snapshot.setup.manifest_reference == "manifest-2"
    assert state.snapshot.setup.manifest_version == 3
    assert state.snapshot.setup.domain_version == 3
    assert state.snapshot.state_version == 5
    assert state.snapshot.last_applied_sequence_no == 7
    assert tuple(event.event_type for event in plan.result_events) == (
        GameEventType.SCRIPT_SET,
    )


@pytest.mark.parametrize(
    ("context_factory", "expected_reason"),
    [
        (
            lambda: setup_context(
                existing=True,
                same_identity=True,
                manifest_version=2,
            ),
            "SCRIPT_ALREADY_SET",
        ),
        (
            lambda: setup_context(existing=True, confirmation=None),
            "SCRIPT_REPLACEMENT_NOT_CONFIRMED",
        ),
    ],
)
def test_business_reject_commits_rejection_without_snapshot_visibility(
    context_factory,
    expected_reason: str,
) -> None:
    (
        _,
        initial,
        envelope,
        port,
        _,
        _,
        _,
        _,
        actor,
        gate,
    ) = _runtime(context_factory=context_factory)
    before = initial.snapshot

    asyncio.run(_drive(gate, envelope))

    state = actor.visible_game_state
    assert gate.lifecycle is GateLifecycle.CLOSED
    assert [call[0] for call in port.calls] == [
        "claim_operation",
        "commit_control_rejection",
    ]
    plan = port.calls[1][1]
    assert isinstance(plan, ControlRejectPlan)
    payload = validate_control_result_event(plan.rejection_event)
    assert isinstance(payload, SessionControlRejectedPayload)
    assert payload.reason_code == expected_reason
    assert state.snapshot is before
    assert state.committed_control_cursor == envelope.event_sequence_no
    assert state.last_completion_identity is not None
    assert (
        state.last_completion_identity.completion_kind
        is ControlCompletionKind.REJECTED
    )
    # The Actor enforces CommittedControlRejectAccepted before returning; the
    # committed reject cursor proves that typed completion path.


def _manifest_anomaly_context():
    context = setup_context(existing=True, manifest_version=3)
    current = context.session_view.current_game_snapshot
    assert current is not None
    return replace(
        context,
        setup_view=replace(
            context.setup_view,
            manifest_reference="manifest-2",
        ),
        session_view=replace(
            context.session_view,
            current_game_snapshot=replace(
                current,
                setup=replace(
                    current.setup,
                    manifest_reference="manifest-2",
                ),
            ),
        ),
    )


@pytest.mark.parametrize(
    (
        "context_factory",
        "evidence_mode",
        "failure_mode",
        "expected_error",
        "expected_reason",
    ),
    [
        (
            lambda: setup_context(
                status=GameSessionStatus.PAUSED,
                phase=GamePhase.LOBBY,
            ),
            None,
            None,
            ControlTurnProcessingError,
            ControlTurnFailureReason.BUILD_NON_COMMIT,
        ),
        (
            _manifest_anomaly_context,
            None,
            None,
            ControlTurnProcessingError,
            ControlTurnFailureReason.BUILD_NON_COMMIT,
        ),
        (
            setup_context,
            "evidence_scope",
            None,
            ActorControlTurnValidationError,
            None,
        ),
        (
            setup_context,
            "evidence_version",
            None,
            ControlTurnProcessingError,
            ControlTurnFailureReason.RECOVERY_REQUIRED,
        ),
        (
            setup_context,
            None,
            "invalid_receipt",
            ControlTurnProcessingError,
            ControlTurnFailureReason.RECEIPT_INVALID,
        ),
    ],
)
def test_fail_closed_fault_stops_before_setup_visibility(
    context_factory,
    evidence_mode: str | None,
    failure_mode: str | None,
    expected_error: type[Exception],
    expected_reason: ControlTurnFailureReason | None,
) -> None:
    (
        _,
        initial,
        envelope,
        port,
        _,
        _,
        _,
        factory,
        actor,
        gate,
    ) = _runtime(
        context_factory=context_factory,
        evidence_mode=evidence_mode,
        failure_mode=failure_mode,
    )

    async def scenario() -> None:
        await gate.start()
        assert await gate.admit(envelope) is GateAdmission.ACCEPTED
        await gate.wait_faulted()

    asyncio.run(scenario())

    assert gate.lifecycle is GateLifecycle.FAULTED
    assert isinstance(gate.fault, expected_error)
    if expected_reason is not None:
        assert isinstance(gate.fault, ControlTurnProcessingError)
        assert gate.fault.reason is expected_reason
    assert gate.failed_envelope is envelope
    assert actor.visible_game_state is initial
    assert actor.visible_game_state.committed_control_cursor == 6
    assert factory.build_calls == [(initial, envelope)]
    if expected_error is ActorControlTurnValidationError:
        expected_calls = []
    elif failure_mode == "invalid_receipt":
        expected_calls = ["claim_operation", "commit_control_apply"]
    else:
        expected_calls = ["claim_operation"]
    assert [call[0] for call in port.calls] == expected_calls


def test_fault_stop_retains_queued_turn_and_never_creates_second_queue() -> None:
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
    ) = _runtime(
        context_factory=setup_context,
        failure_mode="port_failure",
        block_first_claim=True,
    )
    duplicate = envelope

    async def scenario() -> None:
        await gate.start()
        first = asyncio.create_task(gate.admit(envelope))
        await port.first_claim_started.wait()
        assert await first is GateAdmission.ACCEPTED
        assert await gate.admit(duplicate) is GateAdmission.DUPLICATE
        port.release_first_claim.set()
        await gate.wait_faulted()

    asyncio.run(scenario())

    assert gate.lifecycle is GateLifecycle.FAULTED
    assert gate.fault.reason is ControlTurnFailureReason.APPLY_UNKNOWN
    assert actor.visible_game_state is initial
    assert [item for _, item in factory.build_calls] == [envelope]
    assert _owned_async_queues(
        port,
        dispatcher,
        coordinator,
        processor,
        factory,
        actor,
        gate,
    ) == (gate._queue,)
    assert tuple(actor._mailbox) == ()
    assert actor.processed_event_ids == ()


def test_e2e_adapters_have_no_forbidden_runtime_dependencies() -> None:
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
        (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
        )
        for node in ast.walk(
            ast.parse(inspect.getsource(StateBoundSetupEvidenceFactory))
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
