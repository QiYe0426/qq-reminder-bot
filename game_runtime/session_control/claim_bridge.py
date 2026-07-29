"""Actor-turn bridge for acquiring one immutable Control Operation claim."""

from __future__ import annotations

import asyncio
from datetime import datetime
from enum import Enum

from game_runtime.interfaces.ports import SessionControlApplyPort
from game_runtime.session_control.apply_contract import (
    ControlApplyConflict,
    ControlApplyStorageFailure,
    ControlOperationClaim,
)
from game_runtime.session_control.delivery import ControlEventDeliveryEnvelope


class OperationClaimUnknownReason(str, Enum):
    STORAGE_FAILURE = "STORAGE_FAILURE"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    CLAIM_EVIDENCE_MISMATCH = "CLAIM_EVIDENCE_MISMATCH"
    UNEXPECTED_FAILURE = "UNEXPECTED_FAILURE"


class OperationClaimBridgeError(RuntimeError):
    """Base error for a failed claim transfer inside one Actor turn."""


class OperationClaimConflict(OperationClaimBridgeError):
    """A deterministic CAS conflict; execution ownership was not acquired."""

    def __init__(self, conflict: ControlApplyConflict) -> None:
        self.reason = conflict.reason
        self.expected = conflict.expected
        self.actual = conflict.actual
        super().__init__(conflict.reason.value)


class OperationClaimUnknown(OperationClaimBridgeError):
    """The Bridge cannot prove whether execution ownership was acquired."""

    def __init__(self, reason: OperationClaimUnknownReason) -> None:
        if not isinstance(reason, OperationClaimUnknownReason):
            raise TypeError("reason must be an OperationClaimUnknownReason")
        self.reason = reason
        super().__init__(reason.value)


class OperationClaimBridge:
    """Call the Actor-only CAS Port once and validate returned claim evidence."""

    __slots__ = ("_apply_port",)

    def __init__(self, apply_port: SessionControlApplyPort) -> None:
        if not callable(getattr(apply_port, "claim_operation", None)):
            raise TypeError("apply_port must define claim_operation")
        self._apply_port = apply_port

    async def acquire(
        self,
        envelope: ControlEventDeliveryEnvelope,
        claim_id: str,
        claimed_at: datetime,
    ) -> ControlOperationClaim:
        if not isinstance(envelope, ControlEventDeliveryEnvelope):
            raise TypeError("envelope must be a ControlEventDeliveryEnvelope")
        _require_text("claim_id", claim_id)
        _require_time("claimed_at", claimed_at)

        event = envelope.event
        try:
            claim = await self._apply_port.claim_operation(
                game_id=event.game_id,
                session_id=event.session_id,
                command_id=envelope.command_id,
                operation_id=envelope.operation_id,
                input_event_id=event.event_id,
                claim_id=claim_id,
                claimed_at=claimed_at,
            )
        except ControlApplyConflict as exc:
            raise OperationClaimConflict(exc) from exc
        except ControlApplyStorageFailure as exc:
            raise OperationClaimUnknown(
                OperationClaimUnknownReason.STORAGE_FAILURE
            ) from exc
        except asyncio.CancelledError as exc:
            raise OperationClaimUnknown(
                OperationClaimUnknownReason.CANCELLED
            ) from exc
        except TimeoutError as exc:
            raise OperationClaimUnknown(
                OperationClaimUnknownReason.TIMEOUT
            ) from exc
        except Exception as exc:
            raise OperationClaimUnknown(
                OperationClaimUnknownReason.UNEXPECTED_FAILURE
            ) from exc

        expected_claim = ControlOperationClaim(
            game_id=event.game_id,
            session_id=event.session_id,
            command_id=envelope.command_id,
            operation_id=envelope.operation_id,
            input_event_id=event.event_id,
            claim_id=claim_id,
            claimed_at=claimed_at,
        )
        if not isinstance(claim, ControlOperationClaim) or claim != expected_claim:
            raise OperationClaimUnknown(
                OperationClaimUnknownReason.CLAIM_EVIDENCE_MISMATCH
            )
        return claim


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} must not be empty")


def _require_time(name: str, value: object) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
