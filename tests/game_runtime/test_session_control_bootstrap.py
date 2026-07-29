import ast
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from game_runtime.event import (
    EventVisibility,
    GameEvent,
    GameEventSource,
    GameEventType,
    SessionCreatedPayload,
)
from game_runtime.interfaces import SessionControlApplyPort
from game_runtime.participant import ParticipantType
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control import (
    BootstrapIdFactory,
    CandidateSessionSnapshot,
    ControlApplyReceipt,
    ControlOperationStatus,
    CreateSessionBootstrapContext,
    CreateSessionBootstrapPlan,
    CreateSessionWithEventPlan,
    CreationGuard,
    CreationGuardPort,
    CreationGuardReleaseReason,
    DMCommandEventPayload,
    OwnershipIntent,
    OwnershipIntentType,
    ParticipantMutation,
    ParticipantMutationType,
    ProvisionalActorContract,
    ProvisionalActorContractError,
    SessionCommandType,
)


NOW = datetime(2026, 7, 16, 14, 0, tzinfo=timezone.utc)


def context() -> CreateSessionBootstrapContext:
    return CreateSessionBootstrapContext(
        group_id="group-1",
        requester="principal-dm-1",
        command_id="command-create-1",
        correlation_id="correlation-create-1",
        requested_at=NOW,
        proposed_game_id="game-proposed-1",
        proposed_session_id="session-proposed-1",
    )


def guard() -> CreationGuard:
    target = context()
    return CreationGuard(
        reservation_id="reservation-1",
        group_id=target.group_id,
        requester=target.requester,
        command_id=target.command_id,
        game_id=target.proposed_game_id,
        session_id=target.proposed_session_id,
        acquired_at=NOW,
    )


def input_event() -> GameEvent:
    target = guard()
    fingerprint = "a" * 64
    payload = DMCommandEventPayload(
        command_id=target.command_id,
        command_type=SessionCommandType.CREATE_SESSION,
        requester=target.requester,
        causation_event_id="event-bootstrap-request-1",
        observed_state_version=0,
        payload_reference=f"sha256:{fingerprint}",
        payload_fingerprint=fingerprint,
    )
    return GameEvent(
        event_id="event-command-create-1",
        game_id=target.game_id,
        session_id=target.session_id,
        event_type=GameEventType.DM_COMMAND,
        actor=target.requester,
        source=GameEventSource.CONTROL,
        correlation_id="correlation-create-1",
        timestamp=NOW,
        payload=payload.to_mapping(),
        visibility=EventVisibility.DM_CONTROL,
        observed_state_version=0,
        causation_event_id="event-bootstrap-request-1",
    )


def result_event() -> GameEvent:
    target = guard()
    payload = SessionCreatedPayload(
        command_id=target.command_id,
        operation_id="operation-create-1",
        input_event_id="event-command-create-1",
        result_code="CREATED",
        result_state_version=0,
        status=GameSessionStatus.CREATED,
        phase=GamePhase.LOBBY,
    )
    return GameEvent(
        event_id="event-session-created-1",
        game_id=target.game_id,
        session_id=target.session_id,
        event_type=GameEventType.SESSION_CREATED,
        actor="provisional-session-actor",
        source=GameEventSource.CONTROL,
        correlation_id="correlation-create-1",
        timestamp=NOW,
        payload=payload.to_mapping(),
        visibility=EventVisibility.DM_CONTROL,
        observed_state_version=0,
        causation_event_id="event-command-create-1",
    )


def bootstrap_plan() -> CreateSessionBootstrapPlan:
    target = guard()
    return CreateSessionBootstrapPlan(
        guard=target,
        operation_id="operation-create-1",
        operation_claim_id="claim-create-1",
        candidate_snapshot=CandidateSessionSnapshot(
            game_id=target.game_id,
            session_id=target.session_id,
            group_id=target.group_id,
            dm_participant_id="participant-dm-1",
            status=GameSessionStatus.CREATED,
            current_phase=GamePhase.LOBBY,
            state_version=0,
            last_applied_sequence_no=0,
        ),
        initial_dm_participant=ParticipantMutation(
            mutation_type=ParticipantMutationType.UPSERT,
            participant_id="participant-dm-1",
            expected_binding_version=None,
            resulting_binding_version=1,
            participant_type=ParticipantType.DM,
        ),
        input_event=input_event(),
        result_event=result_event(),
        ownership_intent=OwnershipIntent(
            OwnershipIntentType.UNCHANGED,
            expected_generation=None,
            resulting_generation=None,
        ),
        operation_terminal_state=ControlOperationStatus.SUCCESS,
    )


def test_bootstrap_context_is_typed_and_immutable() -> None:
    target = context()

    assert target.proposed_game_id == "game-proposed-1"
    assert target.proposed_session_id == "session-proposed-1"
    with pytest.raises(FrozenInstanceError):
        target.proposed_game_id = "caller-override"  # type: ignore[misc]


class ContractOnlyGuardPort:
    async def acquire(self, target, **kwargs) -> CreationGuard:
        return guard()

    async def check(self, group_id: str) -> CreationGuard | None:
        return guard() if group_id == "group-1" else None

    async def release(self, target, **kwargs) -> None:
        return None


def test_group_creation_guard_is_a_structural_contract() -> None:
    port = ContractOnlyGuardPort()

    assert isinstance(port, CreationGuardPort)
    assert callable(port.acquire)
    assert callable(port.check)
    assert callable(port.release)
    assert CreationGuardReleaseReason.COMMITTED.value == "COMMITTED"


def test_runtime_generates_game_and_session_ids() -> None:
    generated = BootstrapIdFactory().generate()
    another = BootstrapIdFactory().generate()

    assert generated.game_id.startswith("game-")
    assert generated.session_id.startswith("session-")
    assert generated != another


def test_provisional_actor_accepts_only_reserved_create_event() -> None:
    provisional = ProvisionalActorContract(guard())
    event = input_event()

    provisional.validate_event(event)
    assert not provisional.registry_publishable
    assert not provisional.has_routing_ownership
    assert provisional.allowed_command_type is SessionCommandType.CREATE_SESSION

    with pytest.raises(ProvisionalActorContractError, match="DM_COMMAND"):
        provisional.validate_event(
            replace(event, event_type=GameEventType.MESSAGE_RECEIVED)
        )
    changed_payload = dict(event.payload)
    changed_payload["command_type"] = SessionCommandType.PAUSE_GAME.value
    with pytest.raises(ProvisionalActorContractError, match="CREATE_SESSION"):
        provisional.validate_event(replace(event, payload=changed_payload))


class ContractOnlyAtomicPort:
    async def claim_operation(self, **kwargs):
        raise AssertionError("not called")

    async def commit_control_apply(self, plan, claim):
        raise AssertionError("not called")

    async def commit_control_rejection(self, plan, claim):
        raise AssertionError("not called")

    async def create_session_with_event(
        self, plan: CreateSessionWithEventPlan
    ) -> ControlApplyReceipt:
        raise AssertionError("contract only; no transaction implementation")


def test_create_session_with_event_contract_accepts_typed_bootstrap_plan() -> None:
    plan = bootstrap_plan()
    port = ContractOnlyAtomicPort()

    assert isinstance(plan, CreateSessionWithEventPlan)
    assert isinstance(port, SessionControlApplyPort)
    assert plan.operation_terminal_state is ControlOperationStatus.SUCCESS
    assert plan.ownership_intent.intent_type is OwnershipIntentType.UNCHANGED


def test_bootstrap_contract_has_no_sqlite_or_runtime_actor_dependency() -> None:
    paths = (
        Path("game_runtime/session_control/bootstrap.py"),
        Path("game_runtime/interfaces/ports.py"),
    )
    forbidden = (
        "sqlite3",
        "aiosqlite",
        "game_runtime.persistence",
        "game_runtime.actor",
        "game_runtime.routing",
    )
    imports: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)

    assert not [name for name in imports if name.startswith(forbidden)]
