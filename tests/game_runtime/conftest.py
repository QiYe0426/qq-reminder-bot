import pytest

from game_runtime.identity import DMIdentity
from game_runtime.participant import ParticipantReference, ParticipantType
from game_runtime.session import GameSession


def make_session(game_id: str = "game-1") -> GameSession:
    dm = DMIdentity(participant_id="participant-dm", qq_id="10001")
    participants = (
        ParticipantReference(
            participant_id="participant-dm",
            qq_id="10001",
            participant_type=ParticipantType.DM,
        ),
        ParticipantReference(
            participant_id="participant-player",
            qq_id="10002",
            participant_type=ParticipantType.PLAYER,
            character_id="detective",
        ),
    )
    return GameSession(
        game_id=game_id,
        session_id=f"session-{game_id}",
        group_id="20001",
        dm_identity=dm,
        participant_references=participants,
    )


@pytest.fixture
def session_factory():
    return make_session
