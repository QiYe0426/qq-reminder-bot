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
    SequenceConflictError,
    SessionAsyncGate,
)
from game_runtime.actor.session_actor import GameSessionActor
from game_runtime.event import (
    GameEventType,
    RuleSetActivatedPayload,
    SessionControlRejectedPayload,
    validate_control_result_event,
)
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control import (
    ActorOwnedApplyCoordinator,
    ActorOwnedGameStateCompletionBoundary,
    ActorValidatedControlTurnEvidence,
    ActorVisibleGameState,
    CompositeGameControlApplyPlanBuilder,
    ControlApplyReceipt,
    ControlClaimAttemptEvidence,
    ControlCompletionKind,
    ControlEventDeliveryEnvelope,
    ControlGameRuleApplyEvidence,
    ControlTurnFailureReason,
    ControlTurnProcessingError,
    ControlTurnProcessor,
    GameRuleEvidenceStatus,
    OwnershipIntentType,
)
from game_runtime.session_control.apply_contract import (
    ControlApplyConflict,
    ControlApplyConflictReason,
    ControlApplyPlan,
    ControlOperationClaim,
    ControlRejectPlan,
)
from test_actor_control_turn_integration_contract import make_session
from test_game_rule_control_plane_contract import _context as game_rule_context
from test_phase_apply_end_to_end_contract import (
    StrictRecordingAtomicApplyPort,
    _owned_async_queues,
)


class ObservingGameRuleAtomicApplyPort(StrictRecordingAtomicApplyPort):
    """Atomic Apply Port fake recording visibility and terminal receipts."""

    __slots__ = (
        "claim_conflict",
        "receipts",
        "visible_state_supplier",
        "visible_states_at_commit",
    )

    def __init__(
        self,
        *,
        ownership_generation: int | None,
        failure_mode: str | None = None,
        receipt_corruption: str | None = None,
        block_first_claim: bool = False,
        claim_conflict: bool = False,
    ) -> None:
        super().__init__(
            ownership_generation=ownership_generation,
            failure_mode=failure_mode,
            receipt_corruption=receipt_corruption,
            block_first_claim=block_first_claim,
        )
        self.claim_conflict = claim_conflict
        self.receipts: list[ControlApplyReceipt] = []
        self.visible_state_supplier = None
        self.visible_states_at_commit: list[ActorVisibleGameState] = []

    async def claim_operation(self, **values: object) -> ControlOperationClaim:
        if self.claim_conflict:
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
            raise ControlApplyConflict(
                ControlApplyConflictReason.IDEMPOTENCY_CONFLICT,
                expected=values["operation_id"],
                actual="operation:already-claimed",
            )
        return await super().claim_operation(**values)

    async def commit_control_apply(
        self,
        plan: ControlApplyPlan,
        claim: ControlOperationClaim,
    ) -> ControlApplyReceipt:
        assert self.visible_state_supplier is not None
        self.visible_states_at_commit.append(self.visible_state_supplier())
        return await super().commit_control_apply(plan, claim)

    def _receipt(self, **values: object) -> ControlApplyReceipt:
        receipt = super()._receipt(**values)
        self.receipts.append(receipt)
        return receipt


class StateBoundGameRuleEvidenceFactory:
    """Synchronous adapter fake binding activation Evidence to Actor truth."""

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
        if self.mode == "missing":
            session_view = replace(
                session_view,
                game_rule_evidence=ControlGameRuleApplyEvidence(),
            )
        elif self.mode in {
            "unknown",
            "manifest_mismatch",
            "scope_mismatch",
            "version_mismatch",
        }:
            activation = session_view.game_rule_evidence.rule_set_activation
            assert activation is not None
            attribute, value = {
                "unknown": (
                    "validation_status",
                    GameRuleEvidenceStatus.UNKNOWN,
                ),
                "manifest_mismatch": (
                    "setup_manifest_reference",
                    "manifest:wrong",
                ),
                "scope_mismatch": ("session_id", "session:wrong"),
                "version_mismatch": (
                    "rule_set_version",
                    activation.rule_set_version + 1,
                ),
            }[self.mode]
            object.__setattr__(activation, attribute, value)
        elif self.mode == "partial_binding":
            object.__setattr__(
                snapshot,
                "game_rules",
                replace(
                    snapshot.game_rules,
                    domain_version=1,
                    committed_rule_set_reference="rule-set:partial",
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
            setup_participant_evidence=context.setup_participant_evidence,
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


def _runtime(
    *,
    context_factory=game_rule_context,
    evidence_mode: str | None = None,
    failure_mode: str | None = None,
    receipt_corruption: str | None = None,
    claim_conflict: bool = False,
    block_first_claim: bool = False,
):
    context = context_factory()
    initial = _initial_state(context)
    envelope = context.envelope
    port = ObservingGameRuleAtomicApplyPort(
        ownership_generation=initial.ownership_generation,
        failure_mode=failure_mode,
        receipt_corruption=receipt_corruption,
        claim_conflict=claim_conflict,
        block_first_claim=block_first_claim,
    )
    dispatcher = CompositeGameControlApplyPlanBuilder()
    coordinator = ActorOwnedApplyCoordinator(
        apply_port=port,
        builder=dispatcher,
    )
    processor = ControlTurnProcessor(coordinator)
    factory = StateBoundGameRuleEvidenceFactory(
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
    port.visible_state_supplier = lambda: actor.visible_game_state
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


def test_activation_commits_atomically_through_one_real_control_chain() -> None:
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
    ) = _runtime()
    before = initial.snapshot

    asyncio.run(_drive(gate, envelope))

    state = actor.visible_game_state
    assert gate.lifecycle is GateLifecycle.CLOSED
    assert [call[0] for call in port.calls] == [
        "claim_operation",
        "commit_control_apply",
    ]
    claim_values = port.calls[0][1]
    plan = port.calls[1][1]
    assert isinstance(claim_values, dict)
    assert isinstance(plan, ControlApplyPlan)
    assert claim_values["claim_id"] == plan.operation_claim_id
    assert port.visible_states_at_commit == [initial]
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

    activation = (
        factory.evidence[0]
        .session_view.game_rule_evidence.rule_set_activation
    )
    assert activation is not None
    assert activation.validation_status is GameRuleEvidenceStatus.VERIFIED
    assert (
        activation.setup_manifest_reference
        == before.setup.manifest_reference
        == factory.evidence[0].setup_view.manifest_reference
    )
    assert activation.setup_version == before.setup.domain_version

    candidate = state.snapshot
    assert candidate is plan.candidate_snapshot
    assert candidate is not before
    assert candidate.state_version == before.state_version + 1
    assert candidate.last_applied_sequence_no == envelope.event_sequence_no
    assert state.committed_control_cursor == envelope.event_sequence_no
    assert candidate.lifecycle is before.lifecycle
    assert candidate.phase is before.phase
    assert candidate.setup is before.setup
    assert candidate.participants is before.participants
    assert candidate.game_rules is not before.game_rules
    assert candidate.hidden_state is not before.hidden_state
    assert candidate.game_rules.domain_version == (
        before.game_rules.domain_version + 1
    )
    assert candidate.hidden_state.domain_version == (
        before.hidden_state.domain_version + 1
    )
    assert (
        candidate.game_rules.committed_rule_set_reference
        == activation.committed_rule_set_reference
    )
    assert (
        candidate.hidden_state.committed_state_reference
        == activation.opaque_hidden_state_reference
    )
    assert (
        plan.ownership_intent.intent_type
        is OwnershipIntentType.UNCHANGED
    )
    assert plan.ownership_intent.expected_generation is None
    assert plan.ownership_intent.resulting_generation is None
    assert state.ownership_generation is None
    assert state.last_completion_identity is not None
    assert (
        state.last_completion_identity.completion_kind
        is ControlCompletionKind.APPLIED
    )

    assert len(port.receipts) == 1
    receipt = port.receipts[0]
    assert receipt.committed_state_version == candidate.state_version
    assert receipt.committed_cursor == envelope.event_sequence_no
    assert len(receipt.result_event_references) == 1
    assert (
        receipt.result_event_references[0].event_type
        is GameEventType.RULE_SET_ACTIVATED
    )
    assert tuple(event.event_type for event in plan.result_events) == (
        GameEventType.RULE_SET_ACTIVATED,
    )
    payload = validate_control_result_event(plan.result_events[0])
    assert isinstance(payload, RuleSetActivatedPayload)
    assert payload.result_code == "RULE_SET_ACTIVATED"


@pytest.mark.parametrize(
    ("context_factory", "reason"),
    [
        (lambda: game_rule_context(setup_ready=False), "SETUP_NOT_READY"),
        (
            lambda: game_rule_context(
                status=GameSessionStatus.RUNNING,
                phase=GamePhase.INTRODUCTION,
            ),
            "INVALID_LIFECYCLE",
        ),
        (
            lambda: game_rule_context(
                current_rule_reference="rule-set:commit-1",
                current_hidden_reference="hidden-state:commit-1",
            ),
            "RULE_SET_ALREADY_ACTIVE",
        ),
        (
            lambda: game_rule_context(
                current_rule_reference="rule-set:current",
                current_hidden_reference="hidden-state:current",
            ),
            "RULE_SET_CONFLICT",
        ),
    ],
)
def test_business_reject_commits_cursor_without_snapshot_replacement(
    context_factory,
    reason: str,
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
    assert payload.reason_code == reason
    assert factory.build_calls == [(initial, envelope)]
    assert state.snapshot is before
    assert state.snapshot.state_version == before.state_version
    assert (
        state.snapshot.last_applied_sequence_no
        == before.last_applied_sequence_no
    )
    assert state.committed_control_cursor == envelope.event_sequence_no
    assert state.ownership_generation is None
    assert state.last_completion_identity is not None
    assert (
        state.last_completion_identity.completion_kind
        is ControlCompletionKind.REJECTED
    )


@pytest.mark.parametrize(
    (
        "evidence_mode",
        "failure_mode",
        "receipt_corruption",
        "claim_conflict",
        "expected_reason",
        "expected_calls",
    ),
    [
        (
            "missing",
            None,
            None,
            False,
            ControlTurnFailureReason.BUILD_NON_COMMIT,
            ["claim_operation"],
        ),
        (
            "unknown",
            None,
            None,
            False,
            ControlTurnFailureReason.BUILD_NON_COMMIT,
            ["claim_operation"],
        ),
        (
            "manifest_mismatch",
            None,
            None,
            False,
            ControlTurnFailureReason.BUILD_NON_COMMIT,
            ["claim_operation"],
        ),
        (
            "scope_mismatch",
            None,
            None,
            False,
            ControlTurnFailureReason.BUILD_NON_COMMIT,
            ["claim_operation"],
        ),
        (
            "version_mismatch",
            None,
            None,
            False,
            ControlTurnFailureReason.BUILD_NON_COMMIT,
            ["claim_operation"],
        ),
        (
            "partial_binding",
            None,
            None,
            False,
            ControlTurnFailureReason.BUILD_NON_COMMIT,
            ["claim_operation"],
        ),
        (
            None,
            None,
            None,
            True,
            ControlTurnFailureReason.CLAIM_CONFLICT,
            ["claim_operation"],
        ),
        (
            None,
            "port_failure",
            None,
            False,
            ControlTurnFailureReason.APPLY_UNKNOWN,
            ["claim_operation", "commit_control_apply"],
        ),
        (
            None,
            "invalid_receipt",
            None,
            False,
            ControlTurnFailureReason.RECEIPT_INVALID,
            ["claim_operation", "commit_control_apply"],
        ),
        (
            None,
            None,
            "event_id",
            False,
            ControlTurnFailureReason.RECEIPT_INVALID,
            ["claim_operation", "commit_control_apply"],
        ),
    ],
)
def test_fail_closed_fault_stops_without_speculative_visibility_or_retry(
    evidence_mode: str | None,
    failure_mode: str | None,
    receipt_corruption: str | None,
    claim_conflict: bool,
    expected_reason: ControlTurnFailureReason,
    expected_calls: list[str],
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
        evidence_mode=evidence_mode,
        failure_mode=failure_mode,
        receipt_corruption=receipt_corruption,
        claim_conflict=claim_conflict,
    )

    async def scenario() -> None:
        await gate.start()
        assert await gate.admit(envelope) is GateAdmission.ACCEPTED
        await gate.wait_faulted()

    asyncio.run(scenario())

    assert gate.lifecycle is GateLifecycle.FAULTED
    assert isinstance(gate.fault, ControlTurnProcessingError)
    assert gate.fault.reason is expected_reason
    assert gate.failed_envelope is envelope
    assert actor.visible_game_state is initial
    assert actor.visible_game_state.committed_control_cursor == 6
    assert factory.build_calls == [(initial, envelope)]
    assert [call[0] for call in port.calls] == expected_calls
    assert [call[0] for call in port.calls].count("claim_operation") == 1
    assert (
        [call[0] for call in port.calls].count("commit_control_apply")
        <= 1
    )


def test_exact_duplicate_delivery_executes_once_on_the_existing_gate() -> None:
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
    ) = _runtime(block_first_claim=True)

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
    assert factory.build_calls == [(initial, envelope)]
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


def test_conflicting_duplicate_faults_without_a_second_execution_plane() -> None:
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
    ) = _runtime(block_first_claim=True)
    conflicting = replace(
        envelope,
        operation_id="operation-conflicting",
    )

    async def scenario() -> None:
        await gate.start()
        first = asyncio.create_task(gate.admit(envelope))
        await port.first_claim_started.wait()
        assert await first is GateAdmission.ACCEPTED
        with pytest.raises(SequenceConflictError):
            await gate.admit(conflicting)
        port.release_first_claim.set()
        await gate.wait_faulted()

    asyncio.run(scenario())

    assert gate.lifecycle is GateLifecycle.FAULTED
    assert isinstance(gate.fault, SequenceConflictError)
    assert factory.build_calls == [(initial, envelope)]
    assert [call[0] for call in port.calls] == [
        "claim_operation",
        "commit_control_apply",
    ]
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


def test_hidden_activation_material_never_enters_public_completion_contracts() -> None:
    (
        _,
        _,
        envelope,
        port,
        _,
        _,
        _,
        _,
        actor,
        gate,
    ) = _runtime()

    asyncio.run(_drive(gate, envelope))

    plan = port.calls[1][1]
    assert isinstance(plan, ControlApplyPlan)
    activation = plan.game_rule_evidence.rule_set_activation
    assert activation is not None
    hidden_material = (
        activation.opaque_hidden_state_reference,
        activation.committed_rule_set_reference,
        "opaque_hidden_state_reference",
    )
    result_surface = repr(plan.result_events[0])
    completion_surface = repr(actor.visible_game_state.last_completion_identity)
    receipt_surface = repr(port.receipts[0].result_event_references)
    for secret in hidden_material:
        assert secret not in result_surface
        assert secret not in completion_surface
        assert secret not in receipt_surface

    reject_runtime = _runtime(
        context_factory=lambda: game_rule_context(setup_ready=False)
    )
    reject_envelope = reject_runtime[2]
    reject_port = reject_runtime[3]
    reject_actor = reject_runtime[8]
    reject_gate = reject_runtime[9]
    asyncio.run(_drive(reject_gate, reject_envelope))
    reject_plan = reject_port.calls[1][1]
    assert isinstance(reject_plan, ControlRejectPlan)
    reject_surface = repr(reject_plan.rejection_event)
    reject_completion_surface = repr(
        reject_actor.visible_game_state.last_completion_identity
    )
    for secret in hidden_material:
        assert secret not in reject_surface
        assert secret not in reject_completion_surface


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
            ast.parse(inspect.getsource(StateBoundGameRuleEvidenceFactory))
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
    port_methods = {
        node.name
        for node in ast.walk(
            ast.parse(inspect.getsource(ObservingGameRuleAtomicApplyPort))
        )
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert port_methods == {
        "__init__",
        "claim_operation",
        "commit_control_apply",
        "_receipt",
    }
    runtime_calls = [
        node.func.id
        for node in ast.walk(ast.parse(inspect.getsource(_runtime)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert runtime_calls.count("CompositeGameControlApplyPlanBuilder") == 1
    assert runtime_calls.count("ActorOwnedApplyCoordinator") == 1
    assert runtime_calls.count("ControlTurnProcessor") == 1
    assert runtime_calls.count("SessionAsyncGate") == 1
