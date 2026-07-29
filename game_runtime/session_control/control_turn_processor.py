"""Stateless Processor joining Coordinator outcomes to Receipt validation."""

from __future__ import annotations

import asyncio

from game_runtime.session_control.apply_coordinator import (
    ActorOwnedApplyCoordinator,
    CoordinatorApplyConflict,
    CoordinatorApplyUnknown,
    CoordinatorClaimConflict,
    CoordinatorClaimUnknown,
    CoordinatorNonCommit,
)
from game_runtime.session_control.control_turn_contract import (
    ActorControlTurnValidationError,
    ControlTurnCommitReady,
    ControlTurnFailureReason,
    ControlTurnProcessingError,
)
from game_runtime.session_control.coordinator_evidence import (
    ActorValidatedControlTurnEvidence,
    CoordinatorCommitReturned,
)
from game_runtime.session_control.receipt_validation import (
    ReceiptAccepted,
    validate_control_receipt,
)


class ControlTurnProcessor:
    """Invoke the existing Coordinator once and validate its Receipt handoff."""

    __slots__ = ("_coordinator",)

    def __init__(self, coordinator: ActorOwnedApplyCoordinator) -> None:
        if not callable(getattr(coordinator, "coordinate", None)):
            raise TypeError("coordinator must define coordinate")
        self._coordinator = coordinator

    async def process(
        self,
        evidence: ActorValidatedControlTurnEvidence,
    ) -> ControlTurnCommitReady:
        if not isinstance(evidence, ActorValidatedControlTurnEvidence):
            raise ActorControlTurnValidationError(
                "evidence must be ActorValidatedControlTurnEvidence"
            )
        try:
            outcome = await self._coordinator.coordinate(evidence)
        except asyncio.CancelledError as exc:
            raise ControlTurnProcessingError(
                ControlTurnFailureReason.RECOVERY_REQUIRED
            ) from exc
        except Exception as exc:
            raise ControlTurnProcessingError(
                ControlTurnFailureReason.RECOVERY_REQUIRED
            ) from exc

        if isinstance(outcome, CoordinatorCommitReturned):
            validation = validate_control_receipt(outcome)
            if not isinstance(validation, ReceiptAccepted):
                raise ControlTurnProcessingError(
                    ControlTurnFailureReason.RECEIPT_INVALID
                )
            return ControlTurnCommitReady(
                build_outcome=outcome.build_outcome,
                accepted_receipt=validation,
            )
        if isinstance(outcome, CoordinatorClaimConflict):
            reason = ControlTurnFailureReason.CLAIM_CONFLICT
        elif isinstance(outcome, CoordinatorClaimUnknown):
            reason = ControlTurnFailureReason.CLAIM_UNKNOWN
        elif isinstance(outcome, CoordinatorNonCommit):
            reason = ControlTurnFailureReason.BUILD_NON_COMMIT
        elif isinstance(outcome, CoordinatorApplyConflict):
            reason = ControlTurnFailureReason.APPLY_CONFLICT
        elif isinstance(outcome, CoordinatorApplyUnknown):
            reason = ControlTurnFailureReason.APPLY_UNKNOWN
        else:
            reason = ControlTurnFailureReason.RECOVERY_REQUIRED
        raise ControlTurnProcessingError(reason)
