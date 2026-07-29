"""Immutable notification evidence for an already committed Result Event."""

from __future__ import annotations

from dataclasses import dataclass

from game_runtime.event import GameEvent, validate_control_result_event
from game_runtime.session_control.apply_contract import CommittedResultEventReference
from game_runtime.session_control.receipt_validation import ReceiptAccepted


class ResultEventNotificationContractError(ValueError):
    """Raised when notification references do not bind to the committed Event."""


@dataclass(frozen=True, slots=True)
class CommittedResultEventNotification:
    event: GameEvent
    event_reference: CommittedResultEventReference
    operation_reference: str
    input_event_reference: str
    commit_evidence_reference: str

    def __post_init__(self) -> None:
        if not isinstance(self.event, GameEvent):
            raise TypeError("event must be a GameEvent")
        if not isinstance(self.event_reference, CommittedResultEventReference):
            raise TypeError(
                "event_reference must be a CommittedResultEventReference"
            )
        for name in (
            "operation_reference",
            "input_event_reference",
            "commit_evidence_reference",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ResultEventNotificationContractError(
                    f"{name} must be non-empty text"
                )

        payload = validate_control_result_event(self.event)
        if (
            self.event_reference.event_id != self.event.event_id
            or self.event_reference.event_type is not self.event.event_type
            or self.event_reference.stored_event_reference != self.event.event_id
        ):
            raise ResultEventNotificationContractError(
                "event reference does not bind to the committed Result Event"
            )
        if payload.operation_id != self.operation_reference:
            raise ResultEventNotificationContractError(
                "operation reference does not match Result Event evidence"
            )
        if (
            payload.input_event_id != self.input_event_reference
            or self.event.causation_event_id != self.input_event_reference
        ):
            raise ResultEventNotificationContractError(
                "input Event reference does not match Result Event evidence"
            )


def create_committed_result_event_notification(
    *,
    accepted_receipt: ReceiptAccepted,
    event: GameEvent,
    event_reference: CommittedResultEventReference,
    commit_evidence_reference: str,
) -> CommittedResultEventNotification:
    """Derive notification evidence from an accepted Receipt without side effects."""

    if not isinstance(accepted_receipt, ReceiptAccepted):
        raise TypeError("accepted_receipt must be a ReceiptAccepted")
    if commit_evidence_reference != accepted_receipt.commit_evidence_reference:
        raise ResultEventNotificationContractError(
            "commit evidence does not match the accepted Receipt"
        )
    if event_reference not in accepted_receipt.result_event_references:
        raise ResultEventNotificationContractError(
            "event reference is not part of the accepted Receipt"
        )
    if event.game_id != accepted_receipt.game_id or event.session_id != accepted_receipt.session_id:
        raise ResultEventNotificationContractError(
            "Result Event scope does not match the accepted Receipt"
        )

    return CommittedResultEventNotification(
        event=event,
        event_reference=event_reference,
        operation_reference=accepted_receipt.operation_id,
        input_event_reference=accepted_receipt.input_event_id,
        commit_evidence_reference=accepted_receipt.commit_evidence_reference,
    )
