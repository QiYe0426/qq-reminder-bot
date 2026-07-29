from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone

import pytest

from game_runtime.event import GameEventType
from game_runtime.persistence import EventProcessingStatus
from game_runtime.recovery.control_recovery_evidence import (
    ControlCommitEvidence,
    ControlCommitTransactionKind,
    ControlInputEventRecoveryEvidence,
    ControlNotificationRecoveryEvidence,
    ControlOperationRecoveryEvidence,
    ControlRecoveryEvidenceError,
    ControlRecoveryEvidenceSnapshot,
    ControlResultEventRecoveryEvidence,
    ControlSnapshotRecoveryEvidence,
    RecoveryCommitted,
    RecoveryFault,
    RecoveryFaultReason,
    RecoveryOutcome,
    RecoveryReadmissionAllowed,
    RecoveryResolutionDisposition,
    RecoveryResolutionRecord,
    RecoveryUnknown,
    RecoveryUnknownReason,
    derive_recovery_resolution_id,
    validate_control_recovery_evidence,
)
from game_runtime.session_control import ControlOperationStatus


NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)


def _operation(
    status: ControlOperationStatus = ControlOperationStatus.SUCCESS,
    *,
    claim_id: str | None = "claim-1",
    claimed_at: datetime | None = NOW,
) -> ControlOperationRecoveryEvidence:
    return ControlOperationRecoveryEvidence(
        operation_id="operation-1",
        game_id="game-1",
        session_id="session-1",
        command_id="command-1",
        input_event_id="input-event-7",
        input_sequence_no=7,
        claim_id=claim_id,
        claimed_at=claimed_at,
        operation_status=status,
        operation_updated_at=NOW,
    )


def _snapshot_view(version: int) -> ControlSnapshotRecoveryEvidence:
    return ControlSnapshotRecoveryEvidence(
        snapshot_reference=f"snapshot-{version}",
        game_id="game-1",
        session_id="session-1",
        state_version=version,
        cursor=7,
    )


def _input(
    processing_status: EventProcessingStatus = EventProcessingStatus.APPLIED,
) -> ControlInputEventRecoveryEvidence:
    return ControlInputEventRecoveryEvidence(
        event_id="input-event-7",
        game_id="game-1",
        session_id="session-1",
        operation_id="operation-1",
        command_id="command-1",
        sequence_no=7,
        observed_state_version=4,
        processing_status=processing_status,
    )


def _result(
    *,
    event_id: str = "result-event-8",
    sequence_no: int = 8,
    state_version: int = 5,
    event_type: GameEventType = GameEventType.SESSION_STARTED,
    processing_status: EventProcessingStatus = EventProcessingStatus.RECEIVED,
) -> ControlResultEventRecoveryEvidence:
    return ControlResultEventRecoveryEvidence(
        event_id=event_id,
        event_type=event_type,
        sequence_no=sequence_no,
        causation_event_id="input-event-7",
        result_state_version=state_version,
        processing_status=processing_status,
        game_id="game-1",
        session_id="session-1",
        operation_id="operation-1",
        command_id="command-1",
    )


def _notification(
    *,
    event_id: str = "result-event-8",
    sequence_no: int = 8,
    processing_status: EventProcessingStatus = EventProcessingStatus.RECEIVED,
) -> ControlNotificationRecoveryEvidence:
    return ControlNotificationRecoveryEvidence(
        event_id=event_id,
        sequence_no=sequence_no,
        processing_status=processing_status,
        last_checkpoint_reference="event-processing-result-event-8",
    )


def _commit(
    kind: ControlCommitTransactionKind = ControlCommitTransactionKind.APPLY,
) -> ControlCommitEvidence:
    rejection = kind is ControlCommitTransactionKind.REJECTION
    return ControlCommitEvidence(
        commit_evidence_reference="commit-operation-1",
        transaction_kind=kind,
        game_id="game-1",
        session_id="session-1",
        operation_id="operation-1",
        command_id="command-1",
        claim_id="claim-1",
        input_event_id="input-event-7",
        expected_state_version=4,
        committed_state_version=4 if rejection else 5,
        expected_cursor=6,
        committed_cursor=7,
        operation_terminal_status=(
            ControlOperationStatus.FAILED
            if rejection
            else ControlOperationStatus.SUCCESS
        ),
        result_event_references=("result-event-8",),
        snapshot_reference="snapshot-4" if rejection else "snapshot-5",
        ownership_generation=3,
        committed_at=NOW,
    )


def _evidence(
    *,
    operation: ControlOperationRecoveryEvidence | None = None,
    snapshot: ControlSnapshotRecoveryEvidence | None = None,
    input_event: ControlInputEventRecoveryEvidence | None = None,
    result_events: tuple[ControlResultEventRecoveryEvidence, ...] | None = None,
    commit: ControlCommitEvidence | None | object = object(),
    notifications: tuple[ControlNotificationRecoveryEvidence, ...] | None = None,
    existing_resolution: RecoveryResolutionRecord | None = None,
) -> ControlRecoveryEvidenceSnapshot:
    operation = operation or _operation()
    snapshot = snapshot or _snapshot_view(5)
    if result_events is None:
        result_events = (_result(),)
    if not isinstance(commit, (ControlCommitEvidence, type(None))):
        commit = _commit()
    if notifications is None:
        notifications = (_notification(),)
    input_status = (
        EventProcessingStatus.RECEIVED
        if commit is None
        else (
            EventProcessingStatus.REJECTED
            if commit.transaction_kind is ControlCommitTransactionKind.REJECTION
            else EventProcessingStatus.APPLIED
        )
    )
    return ControlRecoveryEvidenceSnapshot(
        contract_version=1,
        evidence_snapshot_reference="recovery-evidence-1",
        read_epoch="read-epoch-1",
        captured_at=NOW,
        recovery_barrier_reference="recovery-barrier-1",
        operation_evidence=operation,
        snapshot_evidence=snapshot,
        input_event_evidence=input_event or _input(input_status),
        result_event_evidence=result_events,
        commit_evidence=commit,
        notification_evidence=notifications,
        existing_resolution=existing_resolution,
    )


def test_valid_success_evidence_binds_atomic_commit_facts() -> None:
    evidence = _evidence()

    assert validate_control_recovery_evidence(evidence) is evidence
    assert evidence.commit_evidence is not None
    assert evidence.commit_evidence.operation_terminal_status is ControlOperationStatus.SUCCESS


def test_valid_failed_rejection_keeps_state_version_and_advances_cursor() -> None:
    result = _result(
        event_type=GameEventType.SESSION_CONTROL_REJECTED,
        state_version=4,
    )
    evidence = _evidence(
        operation=_operation(ControlOperationStatus.FAILED),
        snapshot=_snapshot_view(4),
        result_events=(result,),
        commit=_commit(ControlCommitTransactionKind.REJECTION),
    )

    assert validate_control_recovery_evidence(evidence) is evidence
    assert evidence.snapshot_evidence.state_version == 4
    assert evidence.commit_evidence is not None
    assert evidence.commit_evidence.committed_cursor >= evidence.operation_evidence.input_sequence_no


def test_unknown_operation_and_append_only_resolution_are_supported() -> None:
    disposition = RecoveryResolutionDisposition.NO_COMMIT_CONFIRMED
    resolution_id = derive_recovery_resolution_id(
        operation_id="operation-1",
        evidence_snapshot_reference="recovery-evidence-1",
        disposition=disposition,
        commit_evidence_reference=None,
        result_event_references=(),
    )
    resolution = RecoveryResolutionRecord(
        resolution_id=resolution_id,
        operation_id="operation-1",
        game_id="game-1",
        session_id="session-1",
        raw_operation_status=ControlOperationStatus.UNKNOWN,
        resolution_disposition=disposition,
        evidence_snapshot_reference="recovery-evidence-1",
        commit_evidence_reference=None,
        result_event_references=(),
        resolved_at=NOW,
        resolver_authority_reference="trusted-recovery-1",
    )
    evidence = _evidence(
        operation=_operation(ControlOperationStatus.UNKNOWN),
        snapshot=_snapshot_view(4),
        result_events=(),
        commit=None,
        notifications=(),
        existing_resolution=resolution,
    )

    assert validate_control_recovery_evidence(evidence) is evidence
    assert evidence.operation_evidence.operation_status is ControlOperationStatus.UNKNOWN
    assert evidence.existing_resolution is resolution


def test_created_operation_may_exist_without_claim() -> None:
    evidence = _evidence(
        operation=_operation(
            ControlOperationStatus.CREATED,
            claim_id=None,
            claimed_at=None,
        ),
        snapshot=_snapshot_view(4),
        result_events=(),
        commit=None,
        notifications=(),
    )

    assert validate_control_recovery_evidence(evidence) is evidence


def test_executing_operation_without_claim_is_rejected() -> None:
    with pytest.raises(ControlRecoveryEvidenceError, match="claim"):
        _operation(ControlOperationStatus.EXECUTING, claim_id=None)


def test_recovery_operation_claim_identity_and_time_are_bound_together() -> None:
    with pytest.raises(ControlRecoveryEvidenceError):
        _operation(claim_id="claim-1", claimed_at=None)
    with pytest.raises(ControlRecoveryEvidenceError):
        _operation(claim_id=None, claimed_at=NOW)


def test_result_event_sequences_must_be_unique_and_ascending() -> None:
    with pytest.raises(ControlRecoveryEvidenceError, match="sequence"):
        _evidence(
            result_events=(
                _result(event_id="result-event-9", sequence_no=9),
                _result(event_id="result-event-8", sequence_no=8),
            ),
            commit=replace(
                _commit(),
                result_event_references=("result-event-9", "result-event-8"),
            ),
            notifications=(
                _notification(event_id="result-event-9", sequence_no=9),
                _notification(event_id="result-event-8", sequence_no=8),
            ),
        )


def test_scope_mismatch_is_rejected() -> None:
    with pytest.raises(ControlRecoveryEvidenceError, match="scope"):
        _evidence(snapshot=replace(_snapshot_view(5), session_id="session-other"))


def test_snapshot_version_must_match_commit_kind() -> None:
    with pytest.raises(ControlRecoveryEvidenceError, match="version"):
        _evidence(snapshot=replace(_snapshot_view(5), state_version=6))


def test_notification_checkpoint_must_bind_result_event_and_status() -> None:
    evidence = _evidence(
        result_events=(_result(processing_status=EventProcessingStatus.DEFERRED),),
        notifications=(_notification(processing_status=EventProcessingStatus.DEFERRED),),
    )
    assert validate_control_recovery_evidence(evidence) is evidence
    assert validate_control_recovery_evidence(
        _evidence(notifications=())
    ).notification_evidence == ()

    with pytest.raises(ControlRecoveryEvidenceError, match="notification"):
        _evidence(
            notifications=(
                _notification(event_id="different-event", sequence_no=8),
            )
        )


def test_commit_cas_origin_must_match_input_version_and_sequence() -> None:
    with pytest.raises(ControlRecoveryEvidenceError, match="observed.*version"):
        _evidence(
            input_event=replace(_input(), observed_state_version=3),
        )

    with pytest.raises(ControlRecoveryEvidenceError, match="expected cursor"):
        _evidence(
            commit=replace(_commit(), expected_cursor=5),
        )


def test_cancelled_is_not_an_atomic_commit_terminal_status() -> None:
    with pytest.raises(ControlRecoveryEvidenceError, match="FAILED"):
        replace(
            _commit(ControlCommitTransactionKind.REJECTION),
            operation_terminal_status=ControlOperationStatus.CANCELLED,
        )

    with pytest.raises(ControlRecoveryEvidenceError, match="committed terminal"):
        RecoveryCommitted(
            game_id="game-1",
            session_id="session-1",
            operation_id="operation-1",
            evidence_snapshot_reference="recovery-evidence-1",
            commit_evidence_reference="commit-operation-1",
            resolved_terminal_status=ControlOperationStatus.CANCELLED,
            snapshot_reference="snapshot-4",
            committed_state_version=4,
            committed_cursor=7,
            result_event_references=("result-event-8",),
            notification_replay_references=(),
        )


def test_resolution_disposition_must_match_commit_kind() -> None:
    disposition = RecoveryResolutionDisposition.COMMITTED_SUCCESS
    resolution = RecoveryResolutionRecord(
        resolution_id=derive_recovery_resolution_id(
            operation_id="operation-1",
            evidence_snapshot_reference="recovery-evidence-1",
            disposition=disposition,
            commit_evidence_reference="commit-operation-1",
            result_event_references=("result-event-8",),
        ),
        operation_id="operation-1",
        game_id="game-1",
        session_id="session-1",
        raw_operation_status=ControlOperationStatus.UNKNOWN,
        resolution_disposition=disposition,
        evidence_snapshot_reference="recovery-evidence-1",
        commit_evidence_reference="commit-operation-1",
        result_event_references=("result-event-8",),
        resolved_at=NOW,
        resolver_authority_reference="trusted-recovery-1",
    )
    with pytest.raises(ControlRecoveryEvidenceError, match="disposition"):
        _evidence(
            operation=_operation(ControlOperationStatus.UNKNOWN),
            snapshot=_snapshot_view(4),
            result_events=(
                _result(
                    event_type=GameEventType.SESSION_CONTROL_REJECTED,
                    state_version=4,
                ),
            ),
            commit=_commit(ControlCommitTransactionKind.REJECTION),
            existing_resolution=resolution,
        )


def test_no_commit_resolution_cannot_claim_commit_or_result_evidence() -> None:
    disposition = RecoveryResolutionDisposition.NO_COMMIT_CONFIRMED
    with pytest.raises(ControlRecoveryEvidenceError, match="NO_COMMIT"):
        RecoveryResolutionRecord(
            resolution_id=derive_recovery_resolution_id(
                operation_id="operation-1",
                evidence_snapshot_reference="recovery-evidence-1",
                disposition=disposition,
                commit_evidence_reference="commit-operation-1",
                result_event_references=("result-event-8",),
            ),
            operation_id="operation-1",
            game_id="game-1",
            session_id="session-1",
            raw_operation_status=ControlOperationStatus.UNKNOWN,
            resolution_disposition=disposition,
            evidence_snapshot_reference="recovery-evidence-1",
            commit_evidence_reference="commit-operation-1",
            result_event_references=("result-event-8",),
            resolved_at=NOW,
            resolver_authority_reference="trusted-recovery-1",
        )


def test_existing_resolution_may_reference_its_original_evidence_snapshot() -> None:
    disposition = RecoveryResolutionDisposition.NO_COMMIT_CONFIRMED
    resolution = RecoveryResolutionRecord(
        resolution_id=derive_recovery_resolution_id(
            operation_id="operation-1",
            evidence_snapshot_reference="original-recovery-evidence",
            disposition=disposition,
            commit_evidence_reference=None,
            result_event_references=(),
        ),
        operation_id="operation-1",
        game_id="game-1",
        session_id="session-1",
        raw_operation_status=ControlOperationStatus.UNKNOWN,
        resolution_disposition=disposition,
        evidence_snapshot_reference="original-recovery-evidence",
        commit_evidence_reference=None,
        result_event_references=(),
        resolved_at=NOW,
        resolver_authority_reference="trusted-recovery-1",
    )

    evidence = _evidence(
        operation=_operation(ControlOperationStatus.UNKNOWN),
        snapshot=_snapshot_view(4),
        result_events=(),
        commit=None,
        notifications=(),
        existing_resolution=resolution,
    )

    assert validate_control_recovery_evidence(evidence) is evidence


def test_contracts_are_frozen_slotted_and_outcomes_are_closed_values() -> None:
    operation = _operation()
    assert not hasattr(operation, "__dict__")
    with pytest.raises(FrozenInstanceError):
        operation.operation_id = "other"  # type: ignore[misc]

    outcomes: tuple[RecoveryOutcome, ...] = (
        RecoveryCommitted(
            game_id="game-1",
            session_id="session-1",
            operation_id="operation-1",
            evidence_snapshot_reference="recovery-evidence-1",
            commit_evidence_reference="commit-operation-1",
            resolved_terminal_status=ControlOperationStatus.SUCCESS,
            snapshot_reference="snapshot-5",
            committed_state_version=5,
            committed_cursor=7,
            result_event_references=("result-event-8",),
            notification_replay_references=("result-event-8",),
        ),
        RecoveryReadmissionAllowed(
            game_id="game-1",
            session_id="session-1",
            operation_id="operation-1",
            input_event_id="input-event-7",
            evidence_snapshot_reference="recovery-evidence-1",
            stable_claim_id="control-claim-v1:stable",
        ),
        RecoveryUnknown(
            game_id="game-1",
            session_id="session-1",
            operation_id="operation-1",
            evidence_snapshot_reference="recovery-evidence-1",
            reason=RecoveryUnknownReason.INSUFFICIENT_COMMIT_EVIDENCE,
        ),
        RecoveryFault(
            game_id="game-1",
            session_id="session-1",
            operation_id="operation-1",
            evidence_snapshot_reference="recovery-evidence-1",
            reason=RecoveryFaultReason.EVIDENCE_CONTRADICTION,
        ),
    )

    assert len(outcomes) == 4
    assert all(not hasattr(outcome, "__dict__") for outcome in outcomes)
