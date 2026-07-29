import ast
from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone
from pathlib import Path

import pytest

from game_runtime.event import (
    EventVisibility,
    GameEvent,
    GameEventSource,
    GameEventType,
    SessionPausedPayload,
)
from game_runtime.session import GameSessionStatus
from game_runtime.session_control.apply_contract import CommittedResultEventReference
from game_runtime.session_control.result_notification import (
    CommittedResultEventNotification,
    ResultEventNotificationContractError,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def make_event() -> GameEvent:
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


def make_reference() -> CommittedResultEventReference:
    event = make_event()
    return CommittedResultEventReference(
        event_id=event.event_id,
        sequence_no=12,
        event_type=event.event_type,
        stored_event_reference=event.event_id,
    )


def make_notification() -> CommittedResultEventNotification:
    return CommittedResultEventNotification(
        event=make_event(),
        event_reference=make_reference(),
        operation_reference="operation-1",
        input_event_reference="event-command-1",
        commit_evidence_reference="commit-evidence-1",
    )


def test_committed_result_event_notification_is_immutable() -> None:
    notification = make_notification()

    assert not hasattr(notification, "__dict__")
    with pytest.raises(FrozenInstanceError):
        notification.operation_reference = "operation-other"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("override", "value"),
    (
        ("operation_reference", "operation-other"),
        ("input_event_reference", "event-command-other"),
    ),
)
def test_notification_rejects_operation_or_input_reference_mismatch(
    override: str,
    value: str,
) -> None:
    arguments = {
        "event": make_event(),
        "event_reference": make_reference(),
        "operation_reference": "operation-1",
        "input_event_reference": "event-command-1",
        "commit_evidence_reference": "commit-evidence-1",
    }
    arguments[override] = value

    with pytest.raises(ResultEventNotificationContractError):
        CommittedResultEventNotification(**arguments)


def test_notification_rejects_stored_event_reference_mismatch() -> None:
    event = make_event()
    other_reference = CommittedResultEventReference(
        event_id="event-other",
        sequence_no=12,
        event_type=event.event_type,
        stored_event_reference="event-other",
    )

    with pytest.raises(ResultEventNotificationContractError):
        CommittedResultEventNotification(
            event=event,
            event_reference=other_reference,
            operation_reference="operation-1",
            input_event_reference="event-command-1",
            commit_evidence_reference="commit-evidence-1",
        )


def test_notification_contract_has_no_state_writer_responsibility() -> None:
    forbidden = ("snapshot", "state", "plan", "repository", "port", "callback", "task")
    for field in fields(CommittedResultEventNotification):
        contract = f"{field.name} {field.type}".lower()
        assert not any(term in contract for term in forbidden)

    module_path = (
        Path(__file__).parents[2]
        / "game_runtime"
        / "session_control"
        / "result_notification.py"
    )
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "GameEvent"
        for node in ast.walk(tree)
    )
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not called_attributes.intersection(
        {"append", "append_event", "atomic_apply", "commit", "update_state"}
    )
