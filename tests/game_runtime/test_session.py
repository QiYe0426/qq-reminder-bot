import pytest

from game_runtime.errors import InvalidSessionTransition
from game_runtime.session import GamePhase, GameSessionStatus


def test_session_starts_created_and_follows_lifecycle(session_factory) -> None:
    session = session_factory()

    assert session.status is GameSessionStatus.CREATED
    assert session.current_phase is GamePhase.LOBBY
    assert session.state_version == 0

    session.transition_to(GameSessionStatus.RUNNING)
    assert session.status is GameSessionStatus.RUNNING
    assert session.current_phase is GamePhase.INTRODUCTION

    session.transition_to(GameSessionStatus.PAUSED)
    assert session.status is GameSessionStatus.PAUSED
    assert session.current_phase is GamePhase.INTRODUCTION

    session.transition_to(GameSessionStatus.ENDED)
    assert session.status is GameSessionStatus.ENDED
    assert session.current_phase is GamePhase.ENDING
    assert session.state_version == 3


def test_ended_session_cannot_restart(session_factory) -> None:
    session = session_factory()
    session.transition_to(GameSessionStatus.ENDED)

    with pytest.raises(InvalidSessionTransition):
        session.transition_to(GameSessionStatus.RUNNING)
