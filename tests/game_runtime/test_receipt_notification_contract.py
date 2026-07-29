from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timezone

import pytest

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
from game_runtime.session_control.result_notification import (
    ResultEventNotificationContractError,
    create_committed_result_event_notification,
)


NOW = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)


def make_event(*, event_id: str = "event-session-paused") -> GameEvent:
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
        event_id=event_id,
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


def make_reference() -> CommittedResultEventReference:
    event = make_event()
    return CommittedResultEventReference(
        event_id=event.event_id,
        sequence_no=12,
        event_type=event.event_type,
        stored_event_reference=event.event_id,
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
        result_events=(make_event(),),
        operation_terminal_state=ControlOperationStatus.SUCCESS,
    )


def make_receipt() -> ControlApplyReceipt:
    reference = make_reference()
    return ControlApplyReceipt(
        game_id="game-1",
        session_id="session-1",
        committed_state_version=5,
        committed_cursor=11,
        result_event_ids=(reference.event_id,),
        operation_status=ControlOperationStatus.SUCCESS,
        ownership_generation=3,
        commit_evidence_reference="commit-evidence-1",
        command_id="command-1",
        operation_id="operation-1",
        operation_claim_id="claim-1",
        input_event_id="event-command-1",
        input_sequence_no=11,
        result_event_references=(reference,),
    )


def make_handoff(receipt: ControlApplyReceipt | None = None) -> CoordinatorCommitReturned:
    claim = ControlOperationClaim(
        game_id="game-1",
        session_id="session-1",
        command_id="command-1",
        operation_id="operation-1",
        input_event_id="event-command-1",
        claim_id="claim-1",
        claimed_at=NOW,
    )
    return CoordinatorCommitReturned(
        claim=claim,
        build_outcome=BuildPlanReady(plan=make_plan()),
        receipt=receipt or make_receipt(),
    )


def accepted_receipt() -> ReceiptAccepted:
    result = validate_control_receipt(make_handoff())
    assert isinstance(result, ReceiptAccepted)
    return result


def assert_invalid(receipt: ControlApplyReceipt, reason: ReceiptInvalidReason) -> None:
    result = validate_control_receipt(make_handoff(receipt))
    assert isinstance(result, ReceiptInvalid)
    assert result.reason is reason


def test_receipt_accepted_is_immutable() -> None:
    accepted = accepted_receipt()
    with pytest.raises(FrozenInstanceError):
        accepted.operation_id = "operation-other"  # type: ignore[misc]


def test_receipt_accepted_is_slotted_and_contains_complete_validated_commit_proof() -> None:
    accepted = accepted_receipt()
    assert not hasattr(accepted, "__dict__")
    assert {
        "game_id", "session_id", "command_id", "operation_id", "claim_id",
        "input_event_id", "input_sequence_no", "committed_state_version",
        "committed_cursor", "ownership_generation", "commit_evidence_reference",
        "result_event_references",
    }.issubset({field.name for field in fields(ReceiptAccepted)})
    assert accepted.operation_id == "operation-1"
    assert accepted.input_sequence_no == 11
    assert accepted.committed_state_version == 5
    assert accepted.committed_cursor == 11


def test_receipt_validation_accepts_matching_commit_evidence() -> None:
    assert isinstance(validate_control_receipt(make_handoff()), ReceiptAccepted)


def test_receipt_validation_rejects_scope_mismatch() -> None:
    assert_invalid(replace(make_receipt(), game_id="game-other"), ReceiptInvalidReason.SCOPE_MISMATCH)


def test_receipt_validation_rejects_operation_mismatch() -> None:
    assert_invalid(replace(make_receipt(), operation_id="operation-other"), ReceiptInvalidReason.OPERATION_MISMATCH)


def test_receipt_validation_rejects_claim_mismatch() -> None:
    assert_invalid(replace(make_receipt(), operation_claim_id="claim-other"), ReceiptInvalidReason.CLAIM_MISMATCH)


def test_receipt_validation_rejects_input_identity_mismatch() -> None:
    assert_invalid(replace(make_receipt(), input_event_id="event-other"), ReceiptInvalidReason.INPUT_EVENT_MISMATCH)


def test_receipt_validation_rejects_input_sequence_mismatch() -> None:
    assert_invalid(replace(make_receipt(), input_sequence_no=10), ReceiptInvalidReason.INPUT_EVENT_MISMATCH)


def test_receipt_validation_rejects_state_version_mismatch() -> None:
    assert_invalid(replace(make_receipt(), committed_state_version=6), ReceiptInvalidReason.VERSION_MISMATCH)


def test_receipt_validation_rejects_result_event_reference_mismatch() -> None:
    receipt = make_receipt()
    bad_reference = replace(receipt.result_event_references[0], sequence_no=11)
    assert_invalid(
        replace(receipt, result_event_references=(bad_reference,)),
        ReceiptInvalidReason.RESULT_EVENT_REFERENCE_MISMATCH,
    )


def test_notification_is_derived_from_accepted_receipt() -> None:
    accepted = accepted_receipt()
    notification = create_committed_result_event_notification(
        accepted_receipt=accepted,
        event=make_event(),
        event_reference=make_reference(),
        commit_evidence_reference="commit-evidence-1",
    )
    assert notification.operation_reference == accepted.operation_id
    assert notification.input_event_reference == accepted.input_event_id
    assert notification.commit_evidence_reference == accepted.commit_evidence_reference


def test_notification_rejects_commit_evidence_mismatch() -> None:
    with pytest.raises(ResultEventNotificationContractError):
        create_committed_result_event_notification(
            accepted_receipt=accepted_receipt(),
            event=make_event(),
            event_reference=make_reference(),
            commit_evidence_reference="commit-evidence-other",
        )


def test_notification_rejects_event_identity_mismatch() -> None:
    with pytest.raises(ResultEventNotificationContractError):
        create_committed_result_event_notification(
            accepted_receipt=accepted_receipt(),
            event=make_event(event_id="event-other"),
            event_reference=make_reference(),
            commit_evidence_reference="commit-evidence-1",
        )


def test_duplicate_notification_derivation_is_deterministic_checkpoint_evidence() -> None:
    arguments = {
        "accepted_receipt": accepted_receipt(),
        "event": make_event(),
        "event_reference": make_reference(),
        "commit_evidence_reference": "commit-evidence-1",
    }
    first = create_committed_result_event_notification(**arguments)
    second = create_committed_result_event_notification(**arguments)
    assert first == second
    assert not hasattr(first, "delivery_status")
    assert not hasattr(first, "sent")
