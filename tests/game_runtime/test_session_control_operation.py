import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from game_runtime.session_control import (
    ControlOperation,
    ControlOperationConflict,
    ControlOperationStatus,
    InMemoryControlOperationRepository,
    InvalidControlOperationTransition,
    SessionCommandType,
)


NOW = datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc)


def run(coroutine):
    return asyncio.run(coroutine)


def make_operation(
    operation_id: str = "operation-1",
    command_id: str = "command-1",
) -> ControlOperation:
    return ControlOperation(
        operation_id=operation_id,
        command_id=command_id,
        command_type=SessionCommandType.PAUSE_GAME,
        game_id="game-1",
        session_id="session-game-1",
        group_id="group-1",
        requester="principal-dm-1",
        binding_version=2,
        payload_fingerprint="a" * 64,
        confirmation_reference=None,
        input_event_id="event-command-1",
        result_event_id=None,
        observed_state_version=4,
        result_state_version=None,
        created_at=NOW,
        updated_at=NOW,
    )


def claim(
    repository: InMemoryControlOperationRepository,
    operation_id: str = "operation-1",
    claim_id: str = "claim-1",
):
    return repository.claim_operation(
        "game-1",
        operation_id,
        claim_id=claim_id,
        claimed_at=NOW + timedelta(seconds=1),
    )


def test_operation_is_created_and_loaded_by_explicit_game_scope() -> None:
    repository = InMemoryControlOperationRepository()
    operation = make_operation()

    created = run(repository.create_operation(operation))
    restored = run(repository.get_operation("game-1", operation.operation_id))

    assert created.status is ControlOperationStatus.CREATED
    assert restored == created
    assert run(repository.get_operation("game-other", operation.operation_id)) is None


def test_command_id_is_idempotent_and_reuses_existing_operation() -> None:
    repository = InMemoryControlOperationRepository()
    original = make_operation()
    duplicate = replace(original, operation_id="operation-retry")

    first = run(repository.create_operation(original))
    second = run(repository.create_operation(duplicate))

    assert second == first
    assert second.operation_id == "operation-1"
    assert run(repository.get_by_command_id("game-1", "command-1")) == first

    changed = replace(duplicate, payload_fingerprint="b" * 64)
    with pytest.raises(ControlOperationConflict, match="command_id"):
        run(repository.create_operation(changed))


def test_created_executing_success_transition_records_result_evidence() -> None:
    repository = InMemoryControlOperationRepository()
    run(repository.create_operation(make_operation()))

    executing = run(claim(repository))
    success = run(
        repository.update_result(
            "game-1",
            "operation-1",
            target_status=ControlOperationStatus.SUCCESS,
            result_code="APPLIED",
            result_event_id="event-result-1",
            result_state_version=5,
            occurred_at=NOW + timedelta(seconds=2),
        )
    )

    assert executing.status is ControlOperationStatus.EXECUTING
    assert success.status is ControlOperationStatus.SUCCESS
    assert success.result_event_id == "event-result-1"
    assert success.result_state_version == 5
    assert success.is_terminal


def test_executing_operation_can_finish_failed() -> None:
    repository = InMemoryControlOperationRepository()
    run(repository.create_operation(make_operation()))
    run(claim(repository))

    failed = run(
        repository.update_result(
            "game-1",
            "operation-1",
            target_status=ControlOperationStatus.FAILED,
            result_code="STALE_VERSION",
            result_event_id="event-rejected-1",
            result_state_version=4,
            occurred_at=NOW + timedelta(seconds=2),
        )
    )

    assert failed.status is ControlOperationStatus.FAILED
    assert failed.result_code == "STALE_VERSION"


def test_executing_operation_can_finish_unknown_without_result_evidence() -> None:
    repository = InMemoryControlOperationRepository()
    run(repository.create_operation(make_operation()))
    run(claim(repository))

    unknown = run(
        repository.update_result(
            "game-1",
            "operation-1",
            target_status=ControlOperationStatus.UNKNOWN,
            result_code="COMMIT_OUTCOME_UNKNOWN",
            occurred_at=NOW + timedelta(seconds=2),
        )
    )

    assert unknown.status is ControlOperationStatus.UNKNOWN
    assert unknown.result_event_id is None
    assert unknown.result_state_version is None
    assert [item.operation_id for item in run(repository.list_recoverable("game-1"))] == [
        "operation-1"
    ]


def test_claim_is_cas_and_rejects_another_actor_turn() -> None:
    repository = InMemoryControlOperationRepository()
    run(repository.create_operation(make_operation()))

    first = run(claim(repository, claim_id="claim-actor-a"))
    duplicate = run(claim(repository, claim_id="claim-actor-a"))
    assert duplicate == first

    with pytest.raises(ControlOperationConflict, match="another Actor"):
        run(claim(repository, claim_id="claim-actor-b"))


def test_illegal_status_transitions_are_rejected() -> None:
    repository = InMemoryControlOperationRepository()
    operation = make_operation()
    run(repository.create_operation(operation))

    with pytest.raises(InvalidControlOperationTransition):
        run(
            repository.update_result(
                "game-1",
                "operation-1",
                target_status=ControlOperationStatus.SUCCESS,
                result_code="APPLIED",
                result_event_id="event-result-1",
                result_state_version=5,
                occurred_at=NOW + timedelta(seconds=1),
            )
        )

    run(claim(repository))
    run(
        repository.update_result(
            "game-1",
            "operation-1",
            target_status=ControlOperationStatus.SUCCESS,
            result_code="APPLIED",
            result_event_id="event-result-1",
            result_state_version=5,
            occurred_at=NOW + timedelta(seconds=2),
        )
    )
    with pytest.raises(ControlOperationConflict):
        run(claim(repository, claim_id="claim-after-terminal"))


def test_recovery_query_supports_created_executing_and_unknown() -> None:
    repository = InMemoryControlOperationRepository()
    created = make_operation("operation-created", "command-created")
    executing = replace(
        make_operation("operation-executing", "command-executing"),
        input_event_id="event-command-executing",
    )
    unknown = replace(
        make_operation("operation-unknown", "command-unknown"),
        input_event_id="event-command-unknown",
    )
    for operation in (created, executing, unknown):
        run(repository.create_operation(operation))

    run(claim(repository, "operation-executing", "claim-executing"))
    run(claim(repository, "operation-unknown", "claim-unknown"))
    run(
        repository.update_result(
            "game-1",
            "operation-unknown",
            target_status=ControlOperationStatus.UNKNOWN,
            result_code="COMMIT_OUTCOME_UNKNOWN",
            occurred_at=NOW + timedelta(seconds=2),
        )
    )

    recoverable = run(repository.list_recoverable("game-1"))

    assert {item.status for item in recoverable} == {
        ControlOperationStatus.CREATED,
        ControlOperationStatus.EXECUTING,
        ControlOperationStatus.UNKNOWN,
    }
