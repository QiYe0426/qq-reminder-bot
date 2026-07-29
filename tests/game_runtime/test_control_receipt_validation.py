from dataclasses import replace
from datetime import datetime, timezone

from game_runtime.event import (
    EventVisibility,
    GameEvent,
    GameEventSource,
    GameEventType,
    SessionPausedPayload,
)
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control.apply_contract import (
    CandidateSessionSnapshot,
    CommittedResultEventReference,
    ControlApplyPlan,
    ControlApplyReceipt,
    ControlOperationClaim,
    OwnershipIntent,
    OwnershipIntentType,
)
from game_runtime.session_control.apply_plan_builder import BuildPlanReady
from game_runtime.session_control.commands import SessionCommandType
from game_runtime.session_control.coordinator_evidence import CoordinatorCommitReturned
from game_runtime.session_control.operation import ControlOperationStatus
from game_runtime.session_control.receipt_validation import (
    ReceiptAccepted,
    ReceiptInvalid,
    ReceiptInvalidReason,
    validate_control_receipt,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def make_result_event() -> GameEvent:
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
    return GameEvent(
        event_id="event-session-paused",
        game_id="game-1",
        session_id="session-1",
        event_type=GameEventType.SESSION_PAUSED,
        actor="session-actor",
        source=GameEventSource.CONTROL,
        correlation_id="correlation-1",
        timestamp=NOW,
        payload=payload.to_mapping(),
        visibility=EventVisibility.SYSTEM_ONLY,
        observed_state_version=4,
        causation_event_id="event-command-1",
    )


def make_claim() -> ControlOperationClaim:
    return ControlOperationClaim(
        game_id="game-1",
        session_id="session-1",
        command_id="command-1",
        operation_id="operation-1",
        input_event_id="event-command-1",
        claim_id="claim-1",
        claimed_at=NOW,
    )


def make_plan() -> ControlApplyPlan:
    return ControlApplyPlan(
        game_id="game-1",
        session_id="session-1",
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
        candidate_snapshot=CandidateSessionSnapshot(
            game_id="game-1",
            session_id="session-1",
            group_id="group-1",
            dm_participant_id="participant-dm",
            status=GameSessionStatus.PAUSED,
            current_phase=GamePhase.EXPLORATION,
            state_version=5,
            last_applied_sequence_no=11,
        ),
        participant_mutations=(),
        setup_mutations=(),
        ownership_intent=OwnershipIntent(
            intent_type=OwnershipIntentType.RETAIN,
            expected_generation=3,
            resulting_generation=3,
        ),
        result_events=(make_result_event(),),
        operation_terminal_state=ControlOperationStatus.SUCCESS,
    )


def make_receipt() -> ControlApplyReceipt:
    result_event = make_result_event()
    return ControlApplyReceipt(
        game_id="game-1",
        session_id="session-1",
        committed_state_version=5,
        committed_cursor=11,
        result_event_ids=(result_event.event_id,),
        operation_status=ControlOperationStatus.SUCCESS,
        ownership_generation=3,
        commit_evidence_reference="commit-evidence-1",
        command_id="command-1",
        operation_id="operation-1",
        operation_claim_id="claim-1",
        input_event_id="event-command-1",
        input_sequence_no=11,
        result_event_references=(
            CommittedResultEventReference(
                event_id=result_event.event_id,
                sequence_no=12,
                event_type=result_event.event_type,
                stored_event_reference=result_event.event_id,
            ),
        ),
    )


def make_handoff(
    receipt: ControlApplyReceipt | None = None,
) -> CoordinatorCommitReturned:
    return CoordinatorCommitReturned(
        claim=make_claim(),
        build_outcome=BuildPlanReady(plan=make_plan()),
        receipt=receipt or make_receipt(),
    )


def assert_invalid(
    receipt: ControlApplyReceipt,
    reason: ReceiptInvalidReason,
) -> None:
    result = validate_control_receipt(make_handoff(receipt))
    assert isinstance(result, ReceiptInvalid)
    assert result.reason is reason


def test_receipt_identity_match_is_accepted() -> None:
    result = validate_control_receipt(make_handoff())

    assert isinstance(result, ReceiptAccepted)
    assert result.commit_evidence_reference == "commit-evidence-1"
    assert result.result_event_references == make_receipt().result_event_references


def test_claim_mismatch_is_invalid() -> None:
    assert_invalid(
        replace(make_receipt(), operation_claim_id="claim-other"),
        ReceiptInvalidReason.CLAIM_MISMATCH,
    )


def test_operation_mismatch_is_invalid() -> None:
    assert_invalid(
        replace(make_receipt(), operation_id="operation-other"),
        ReceiptInvalidReason.OPERATION_MISMATCH,
    )


def test_committed_version_mismatch_is_invalid() -> None:
    assert_invalid(
        replace(make_receipt(), committed_state_version=6),
        ReceiptInvalidReason.VERSION_MISMATCH,
    )


def test_committed_cursor_mismatch_is_invalid() -> None:
    assert_invalid(
        replace(make_receipt(), committed_cursor=12),
        ReceiptInvalidReason.CURSOR_MISMATCH,
    )


def test_result_event_reference_mismatch_is_invalid() -> None:
    receipt = make_receipt()
    mismatched = replace(
        receipt.result_event_references[0],
        event_type=GameEventType.SESSION_RESUMED,
    )
    assert_invalid(
        replace(receipt, result_event_references=(mismatched,)),
        ReceiptInvalidReason.RESULT_EVENT_REFERENCE_MISMATCH,
    )
