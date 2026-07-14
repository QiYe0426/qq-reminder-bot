import pytest

from game_runtime.action import ActionQueue, GameAction, GameActionStatus
from game_runtime.errors import InvalidActionTransition


def test_action_transitions_created_executing_success() -> None:
    action = GameAction(
        action_id="action-1",
        game_id="game-1",
        session_id="session-game-1",
        action_type="SPEAK_PUBLIC",
    )

    action.transition_to(GameActionStatus.EXECUTING)
    action.transition_to(GameActionStatus.SUCCESS)

    assert action.status is GameActionStatus.SUCCESS


def test_action_transitions_executing_unknown_and_stays_terminal() -> None:
    action = GameAction(
        action_id="action-1",
        game_id="game-1",
        session_id="session-game-1",
        action_type="SPEAK_PUBLIC",
    )

    action.transition_to(GameActionStatus.EXECUTING)
    action.transition_to(GameActionStatus.UNKNOWN)

    assert action.status is GameActionStatus.UNKNOWN
    with pytest.raises(InvalidActionTransition):
        action.transition_to(GameActionStatus.SUCCESS)


def test_action_queue_claims_and_completes_without_execution() -> None:
    queue = ActionQueue("game-1", "session-game-1")
    action = GameAction(
        action_id="action-1",
        game_id="game-1",
        session_id="session-game-1",
        action_type="SPEAK_PUBLIC",
    )
    queue.enqueue(action)

    claimed = queue.claim_next()
    assert claimed is action
    assert action.status is GameActionStatus.EXECUTING

    completed = queue.complete("action-1", GameActionStatus.UNKNOWN)
    assert completed.status is GameActionStatus.UNKNOWN
    assert queue.claim_next() is None
