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
    SessionControlRejectedPayload,
    SessionPausedPayload,
)
from game_runtime.interfaces import SessionControlApplyPort
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control import (
    CandidateSessionSnapshot,
    ControlApplyConflict,
    ControlApplyConflictReason,
    ControlApplyPlan,
    ControlApplyReceipt,
    ControlGameRuleApplyEvidence,
    ControlOperationClaim,
    ControlOperationStatus,
    ControlRejectPlan,
    OwnershipIntent,
    OwnershipIntentType,
    SessionCommandType,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def result_event(
    event_type: GameEventType = GameEventType.SESSION_PAUSED,
) -> GameEvent:
    if event_type is GameEventType.SESSION_CONTROL_REJECTED:
        payload = SessionControlRejectedPayload(
            command_id="command-1",
            operation_id="operation-1",
            input_event_id="event-command-1",
            result_code="STALE_VERSION",
            result_state_version=4,
            reason_code="STALE_VERSION",
            state_version=4,
        )
        visibility = EventVisibility.DM_CONTROL
    else:
        payload = SessionPausedPayload(
            command_id="command-1",
            operation_id="operation-1",
            input_event_id="event-command-1",
            result_code="APPLIED",
            result_state_version=5,
            previous_status=GameSessionStatus.RUNNING,
            current_status=GameSessionStatus.PAUSED,
            reason_code="DM_REQUEST",
        )
        visibility = EventVisibility.SYSTEM_ONLY
    return GameEvent(
        event_id=f"event-{event_type.value.lower()}",
        game_id="game-1",
        session_id="session-game-1",
        event_type=event_type,
        actor="session-actor",
        source=GameEventSource.CONTROL,
        correlation_id="correlation-1",
        timestamp=NOW,
        payload=payload.to_mapping(),
        visibility=visibility,
        observed_state_version=4,
        causation_event_id="event-command-1",
    )


def candidate() -> CandidateSessionSnapshot:
    return CandidateSessionSnapshot(
        game_id="game-1",
        session_id="session-game-1",
        group_id="group-1",
        dm_participant_id="participant-dm-1",
        status=GameSessionStatus.PAUSED,
        current_phase=GamePhase.EXPLORATION,
        state_version=5,
        last_applied_sequence_no=11,
    )


def apply_plan() -> ControlApplyPlan:
    return ControlApplyPlan(
        game_id="game-1",
        session_id="session-game-1",
        group_id="group-1",
        command_id="command-1",
        command_type=SessionCommandType.PAUSE_GAME,
        operation_id="operation-1",
        operation_claim_id="claim-1",
        input_event_id="event-command-1",
        input_sequence_no=11,
        expected_state_version=4,
        expected_cursor=10,
        expected_binding_version=2,
        candidate_snapshot=candidate(),
        participant_mutations=(),
        setup_mutations=(),
        ownership_intent=OwnershipIntent(
            OwnershipIntentType.RETAIN,
            expected_generation=3,
            resulting_generation=3,
        ),
        result_events=(result_event(),),
        operation_terminal_state=ControlOperationStatus.SUCCESS,
    )


def claim() -> ControlOperationClaim:
    return ControlOperationClaim(
        game_id="game-1",
        session_id="session-game-1",
        command_id="command-1",
        operation_id="operation-1",
        input_event_id="event-command-1",
        claim_id="claim-1",
        claimed_at=NOW,
    )


def receipt(
    status: ControlOperationStatus = ControlOperationStatus.SUCCESS,
) -> ControlApplyReceipt:
    return ControlApplyReceipt(
        game_id="game-1",
        session_id="session-game-1",
        committed_state_version=5 if status is ControlOperationStatus.SUCCESS else 4,
        committed_cursor=11,
        result_event_ids=("event-result-1",),
        operation_status=status,
        ownership_generation=3,
        commit_evidence_reference="commit-evidence-1",
    )


def test_apply_plan_is_typed_immutable_and_consistent() -> None:
    plan = apply_plan()

    assert plan.candidate_snapshot.state_version == 5
    assert plan.result_events[0].event_type is GameEventType.SESSION_PAUSED
    assert plan.operation_terminal_state is ControlOperationStatus.SUCCESS
    assert plan.game_rule_mutations == ()
    assert plan.game_rule_evidence == ControlGameRuleApplyEvidence()
    with pytest.raises(FrozenInstanceError):
        plan.expected_state_version = 5  # type: ignore[misc]


def test_receipt_is_typed_immutable_commit_evidence() -> None:
    committed = receipt()

    assert committed.committed_state_version == 5
    assert committed.committed_cursor == 11
    assert committed.result_event_ids == ("event-result-1",)
    with pytest.raises(FrozenInstanceError):
        committed.committed_cursor = 12  # type: ignore[misc]


class ContractOnlyApplyPort:
    async def claim_operation(self, **kwargs) -> ControlOperationClaim:
        return claim()

    async def commit_control_apply(
        self, plan: ControlApplyPlan, operation_claim: ControlOperationClaim
    ) -> ControlApplyReceipt:
        return receipt()

    async def commit_control_rejection(
        self, plan: ControlRejectPlan, operation_claim: ControlOperationClaim
    ) -> ControlApplyReceipt:
        return receipt(ControlOperationStatus.FAILED)

    async def create_session_with_event(self, plan) -> ControlApplyReceipt:
        return receipt()


def test_session_control_apply_port_is_structural_contract_only() -> None:
    port = ContractOnlyApplyPort()

    assert isinstance(port, SessionControlApplyPort)
    assert callable(port.claim_operation)
    assert callable(port.commit_control_apply)
    assert callable(port.commit_control_rejection)
    assert callable(port.create_session_with_event)


def test_reject_plan_has_no_candidate_or_business_mutations() -> None:
    plan = ControlRejectPlan(
        game_id="game-1",
        session_id="session-game-1",
        group_id="group-1",
        command_id="command-1",
        command_type=SessionCommandType.PAUSE_GAME,
        operation_id="operation-1",
        operation_claim_id="claim-1",
        input_event_id="event-command-1",
        input_sequence_no=11,
        expected_state_version=4,
        expected_cursor=10,
        expected_binding_version=2,
        rejection_event=result_event(GameEventType.SESSION_CONTROL_REJECTED),
        operation_terminal_state=ControlOperationStatus.FAILED,
    )

    assert plan.rejection_event.event_type is GameEventType.SESSION_CONTROL_REJECTED
    assert not hasattr(plan, "candidate_snapshot")
    assert not hasattr(plan, "participant_mutations")
    assert not hasattr(plan, "ownership_intent")


def test_version_mismatch_is_a_typed_no_commit_conflict() -> None:
    conflict = ControlApplyConflict(
        ControlApplyConflictReason.STATE_VERSION_MISMATCH,
        expected=4,
        actual=5,
    )

    assert conflict.reason is ControlApplyConflictReason.STATE_VERSION_MISMATCH
    assert conflict.expected == 4
    assert conflict.actual == 5
    with pytest.raises(ValueError, match="state_version"):
        replace(
            apply_plan(),
            candidate_snapshot=replace(candidate(), state_version=6),
        )


def test_building_plan_does_not_mutate_authoritative_state(session_factory) -> None:
    authoritative = session_factory()
    before = (
        authoritative.status,
        authoritative.current_phase,
        authoritative.state_version,
        authoritative.last_applied_sequence_no,
    )

    apply_plan()

    assert (
        authoritative.status,
        authoritative.current_phase,
        authoritative.state_version,
        authoritative.last_applied_sequence_no,
    ) == before


def test_apply_contract_has_no_sqlite_or_persistence_adapter_dependency() -> None:
    paths = (
        Path("game_runtime/session_control/apply_contract.py"),
        Path("game_runtime/interfaces/ports.py"),
    )
    forbidden = ("sqlite3", "aiosqlite", "game_runtime.persistence")
    imports: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)

    assert not [name for name in imports if name.startswith(forbidden)]
