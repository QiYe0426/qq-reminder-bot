"""Control Operation repository port and non-durable reference adapter."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from threading import RLock
from typing import Protocol, Sequence

from game_runtime.session_control.operation import (
    ControlOperation,
    ControlOperationStatus,
)


class ControlOperationRepositoryError(RuntimeError):
    """Base repository contract error."""


class ControlOperationNotFound(ControlOperationRepositoryError):
    """Raised when an Operation is absent from the requested game scope."""


class ControlOperationConflict(ControlOperationRepositoryError):
    """Raised on command reuse, claim, or optimistic status conflict."""


class ControlOperationRepository(Protocol):
    """Persistence Port; a durable adapter requires the deferred migration."""

    async def create_operation(
        self, operation: ControlOperation
    ) -> ControlOperation:
        """Create or return the Operation already bound to command_id."""

    async def get_operation(
        self, game_id: str, operation_id: str
    ) -> ControlOperation | None:
        """Load one Operation inside an explicit game scope."""

    async def get_by_command_id(
        self, game_id: str, command_id: str
    ) -> ControlOperation | None:
        """Load idempotency evidence inside an explicit game scope."""

    async def bind_input_event(
        self,
        game_id: str,
        operation_id: str,
        *,
        input_event_id: str,
        occurred_at: datetime,
    ) -> ControlOperation:
        """Bind the ingested DM_COMMAND Event while status remains CREATED."""

    async def claim_operation(
        self,
        game_id: str,
        operation_id: str,
        *,
        claim_id: str,
        claimed_at: datetime,
    ) -> ControlOperation:
        """CAS claim CREATED -> EXECUTING."""

    async def update_result(
        self,
        game_id: str,
        operation_id: str,
        *,
        target_status: ControlOperationStatus,
        result_code: str,
        occurred_at: datetime,
        result_event_id: str | None = None,
        result_state_version: int | None = None,
    ) -> ControlOperation:
        """Close an EXECUTING Operation with typed result evidence."""

    async def cancel_operation(
        self,
        game_id: str,
        operation_id: str,
        *,
        result_code: str,
        occurred_at: datetime,
    ) -> ControlOperation:
        """Close a CREATED Operation before claim."""

    async def list_recoverable(
        self,
        game_id: str,
    ) -> Sequence[ControlOperation]:
        """Return CREATED, EXECUTING, and UNKNOWN recovery candidates."""


class InMemoryControlOperationRepository:
    """Thread-safe contract adapter; not a substitute for a DB migration."""

    def __init__(self) -> None:
        self._operations: dict[str, ControlOperation] = {}
        self._operation_id_by_command: dict[str, str] = {}
        self._lock = RLock()

    async def create_operation(
        self, operation: ControlOperation
    ) -> ControlOperation:
        if not isinstance(operation, ControlOperation):
            raise TypeError("operation must be a ControlOperation")
        if operation.status is not ControlOperationStatus.CREATED:
            raise ValueError("only CREATED Operations can be created")
        with self._lock:
            existing_id = self._operation_id_by_command.get(operation.command_id)
            if existing_id is not None:
                existing = self._operations[existing_id]
                if (
                    existing.idempotency_signature()
                    != operation.idempotency_signature()
                ):
                    raise ControlOperationConflict(
                        "command_id is already bound to different Operation evidence"
                    )
                return _snapshot(existing)
            if operation.operation_id in self._operations:
                raise ControlOperationConflict(
                    "operation_id is already bound to another command"
                )
            stored = _snapshot(operation)
            self._operations[stored.operation_id] = stored
            self._operation_id_by_command[stored.command_id] = stored.operation_id
            return _snapshot(stored)

    async def get_operation(
        self, game_id: str, operation_id: str
    ) -> ControlOperation | None:
        _require_scope_text("game_id", game_id)
        _require_scope_text("operation_id", operation_id)
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None or operation.game_id != game_id:
                return None
            return _snapshot(operation)

    async def get_by_command_id(
        self, game_id: str, command_id: str
    ) -> ControlOperation | None:
        _require_scope_text("game_id", game_id)
        _require_scope_text("command_id", command_id)
        with self._lock:
            operation_id = self._operation_id_by_command.get(command_id)
            if operation_id is None:
                return None
            operation = self._operations[operation_id]
            if operation.game_id != game_id:
                return None
            return _snapshot(operation)

    async def bind_input_event(
        self,
        game_id: str,
        operation_id: str,
        *,
        input_event_id: str,
        occurred_at: datetime,
    ) -> ControlOperation:
        with self._lock:
            operation = self._require_operation(game_id, operation_id)
            operation.bind_input_event(input_event_id, occurred_at=occurred_at)
            return _snapshot(operation)

    async def claim_operation(
        self,
        game_id: str,
        operation_id: str,
        *,
        claim_id: str,
        claimed_at: datetime,
    ) -> ControlOperation:
        with self._lock:
            operation = self._require_operation(game_id, operation_id)
            if operation.status is ControlOperationStatus.EXECUTING:
                if operation.claim_id == claim_id:
                    return _snapshot(operation)
                raise ControlOperationConflict(
                    "Operation has already been claimed by another Actor turn"
                )
            if operation.status is not ControlOperationStatus.CREATED:
                raise ControlOperationConflict(
                    f"Operation cannot be claimed from {operation.status.value}"
                )
            operation.claim(claim_id, claimed_at=claimed_at)
            return _snapshot(operation)

    async def update_result(
        self,
        game_id: str,
        operation_id: str,
        *,
        target_status: ControlOperationStatus,
        result_code: str,
        occurred_at: datetime,
        result_event_id: str | None = None,
        result_state_version: int | None = None,
    ) -> ControlOperation:
        with self._lock:
            operation = self._require_operation(game_id, operation_id)
            operation.complete(
                target_status,
                occurred_at=occurred_at,
                result_code=result_code,
                result_event_id=result_event_id,
                result_state_version=result_state_version,
            )
            return _snapshot(operation)

    async def cancel_operation(
        self,
        game_id: str,
        operation_id: str,
        *,
        result_code: str,
        occurred_at: datetime,
    ) -> ControlOperation:
        with self._lock:
            operation = self._require_operation(game_id, operation_id)
            operation.cancel(occurred_at=occurred_at, result_code=result_code)
            return _snapshot(operation)

    async def list_recoverable(
        self,
        game_id: str,
    ) -> tuple[ControlOperation, ...]:
        _require_scope_text("game_id", game_id)
        recoverable = {
            ControlOperationStatus.CREATED,
            ControlOperationStatus.EXECUTING,
            ControlOperationStatus.UNKNOWN,
        }
        with self._lock:
            return tuple(
                _snapshot(operation)
                for operation in sorted(
                    self._operations.values(),
                    key=lambda item: (item.created_at, item.operation_id),
                )
                if operation.game_id == game_id and operation.status in recoverable
            )

    def _require_operation(
        self, game_id: str, operation_id: str
    ) -> ControlOperation:
        _require_scope_text("game_id", game_id)
        _require_scope_text("operation_id", operation_id)
        operation = self._operations.get(operation_id)
        if operation is None or operation.game_id != game_id:
            raise ControlOperationNotFound(
                "Control Operation does not exist in the requested game"
            )
        return operation


def _snapshot(operation: ControlOperation) -> ControlOperation:
    return replace(operation)


def _require_scope_text(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
