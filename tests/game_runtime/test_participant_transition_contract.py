from __future__ import annotations

import ast
from dataclasses import fields, is_dataclass, replace
from pathlib import Path

import pytest

from game_runtime.participant import ParticipantMembershipState, ParticipantType
from game_runtime.session_control import (
    AssignCharacterTransitionAccepted,
    AssignCharacterTransitionRejected,
    AssignCharacterTransitionRequest,
    ParticipantTransitionContractError,
    ParticipantTransitionContractFailureReason,
    ParticipantTransitionDecision,
    ParticipantTransitionEffect,
    ParticipantTransitionRejectReason,
    ParticipantTransitionRecordState,
    ReplacePlayerTransitionAccepted,
    ReplacePlayerTransitionRejected,
    ReplacePlayerTransitionRequest,
    ParticipantTransitionRequest,
    transition_participant,
)


PLAYER = ParticipantTransitionRecordState(
    participant_id="player-1",
    participant_type=ParticipantType.PLAYER,
    membership_state=ParticipantMembershipState.ACTIVE,
    character_id=None,
    binding_version=2,
)
PLAYER_WITH_CHARACTER = ParticipantTransitionRecordState(
    participant_id="player-1",
    participant_type=ParticipantType.PLAYER,
    membership_state=ParticipantMembershipState.ACTIVE,
    character_id="character-1",
    binding_version=2,
)
SECOND_PLAYER = ParticipantTransitionRecordState(
    participant_id="player-2",
    participant_type=ParticipantType.PLAYER,
    membership_state=ParticipantMembershipState.ACTIVE,
    character_id=None,
    binding_version=5,
)


def test_assign_character_returns_updated_value_and_effect() -> None:
    decision = transition_participant(
        AssignCharacterTransitionRequest(
            current_participant=PLAYER,
            requested_character_id="character-1",
        )
    )

    assert isinstance(decision, AssignCharacterTransitionAccepted)
    assert decision.previous_participant is PLAYER
    assert decision.resulting_participant == replace(
        PLAYER, character_id="character-1", binding_version=3
    )
    assert decision.effect is ParticipantTransitionEffect.ASSIGN_CHARACTER


@pytest.mark.parametrize(
    ("participant", "character_id", "reason"),
    [
        (
            PLAYER_WITH_CHARACTER,
            "character-1",
            ParticipantTransitionRejectReason.CHARACTER_ALREADY_ASSIGNED,
        ),
        (
            PLAYER_WITH_CHARACTER,
            "character-2",
            ParticipantTransitionRejectReason.CHARACTER_CONFLICT,
        ),
        (
            replace(PLAYER, participant_type=ParticipantType.DM),
            "character-1",
            ParticipantTransitionRejectReason.INVALID_CHARACTER_ASSIGNMENT,
        ),
        (
            replace(
                PLAYER,
                membership_state=ParticipantMembershipState.LEFT,
            ),
            "character-1",
            ParticipantTransitionRejectReason.INVALID_CHARACTER_ASSIGNMENT,
        ),
    ],
)
def test_assign_character_rejects_invalid_record_states(
    participant: ParticipantTransitionRecordState,
    character_id: str,
    reason: ParticipantTransitionRejectReason,
) -> None:
    decision = transition_participant(
        AssignCharacterTransitionRequest(
            current_participant=participant,
            requested_character_id=character_id,
        )
    )

    assert isinstance(decision, AssignCharacterTransitionRejected)
    assert decision.current_participant is participant
    assert decision.requested_character_id == character_id
    assert decision.reason is reason


def test_replace_player_transfers_character_and_versions() -> None:
    decision = transition_participant(
        ReplacePlayerTransitionRequest(
            old_participant=PLAYER_WITH_CHARACTER,
            new_participant=SECOND_PLAYER,
        )
    )

    assert isinstance(decision, ReplacePlayerTransitionAccepted)
    assert decision.resulting_old_participant == replace(
        PLAYER_WITH_CHARACTER,
        membership_state=ParticipantMembershipState.REPLACED,
        character_id=None,
        binding_version=3,
    )
    assert decision.resulting_new_participant == replace(
        SECOND_PLAYER, character_id="character-1", binding_version=6
    )
    assert decision.effect is ParticipantTransitionEffect.REPLACE_PLAYER


def test_replace_player_transfers_empty_character() -> None:
    decision = transition_participant(
        ReplacePlayerTransitionRequest(old_participant=PLAYER, new_participant=SECOND_PLAYER)
    )

    assert isinstance(decision, ReplacePlayerTransitionAccepted)
    assert decision.resulting_old_participant == replace(
        PLAYER,
        membership_state=ParticipantMembershipState.REPLACED,
        binding_version=3,
    )
    assert decision.resulting_new_participant == replace(SECOND_PLAYER, binding_version=6)


@pytest.mark.parametrize(
    "old_participant,new_participant",
    [
        (PLAYER, replace(PLAYER, participant_id="player-1")),
        (replace(PLAYER, participant_type=ParticipantType.DM), SECOND_PLAYER),
        (PLAYER, replace(SECOND_PLAYER, membership_state=ParticipantMembershipState.LEFT)),
        (PLAYER, replace(SECOND_PLAYER, character_id="character-2")),
    ],
)
def test_replace_player_rejects_invalid_record_states(
    old_participant: ParticipantTransitionRecordState,
    new_participant: ParticipantTransitionRecordState,
) -> None:
    if old_participant.participant_id == new_participant.participant_id:
        with pytest.raises(ParticipantTransitionContractError) as error:
            ReplacePlayerTransitionRequest(old_participant, new_participant)
        assert error.value.reason is ParticipantTransitionContractFailureReason.DUPLICATE_PARTICIPANT_ID
        return

    decision = transition_participant(
        ReplacePlayerTransitionRequest(old_participant, new_participant)
    )
    assert isinstance(decision, ReplacePlayerTransitionRejected)
    assert decision.reason is ParticipantTransitionRejectReason.INVALID_REPLACEMENT


@pytest.mark.parametrize(
    "kwargs",
    [
        {"participant_id": " ", "participant_type": ParticipantType.PLAYER, "membership_state": ParticipantMembershipState.ACTIVE, "character_id": None, "binding_version": 0},
        {"participant_id": "player", "participant_type": ParticipantType.PLAYER, "membership_state": ParticipantMembershipState.ACTIVE, "character_id": " ", "binding_version": 0},
        {"participant_id": "player", "participant_type": ParticipantType.PLAYER, "membership_state": ParticipantMembershipState.ACTIVE, "character_id": None, "binding_version": -1},
    ],
)
def test_record_rejects_malformed_semantic_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(ParticipantTransitionContractError) as error:
        ParticipantTransitionRecordState(**kwargs)  # type: ignore[arg-type]
    assert error.value.reason is ParticipantTransitionContractFailureReason.INVALID_RECORD_VALUE


@pytest.mark.parametrize(
    "kwargs",
    [
        {"participant_id": 1, "participant_type": ParticipantType.PLAYER, "membership_state": ParticipantMembershipState.ACTIVE, "character_id": None, "binding_version": 0},
        {"participant_id": "player", "participant_type": "PLAYER", "membership_state": ParticipantMembershipState.ACTIVE, "character_id": None, "binding_version": 0},
        {"participant_id": "player", "participant_type": ParticipantType.PLAYER, "membership_state": "ACTIVE", "character_id": None, "binding_version": 0},
        {"participant_id": "player", "participant_type": ParticipantType.PLAYER, "membership_state": ParticipantMembershipState.ACTIVE, "character_id": 1, "binding_version": 0},
        {"participant_id": "player", "participant_type": ParticipantType.PLAYER, "membership_state": ParticipantMembershipState.ACTIVE, "character_id": None, "binding_version": True},
    ],
)
def test_record_rejects_wrong_value_types(kwargs: dict[str, object]) -> None:
    with pytest.raises(TypeError):
        ParticipantTransitionRecordState(**kwargs)  # type: ignore[arg-type]


def test_requests_reject_malformed_and_wrong_values() -> None:
    with pytest.raises(ParticipantTransitionContractError) as error:
        AssignCharacterTransitionRequest(PLAYER, " ")
    assert error.value.reason is ParticipantTransitionContractFailureReason.INVALID_REQUEST_VALUE

    with pytest.raises(TypeError):
        AssignCharacterTransitionRequest("not-a-record", "character-1")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ReplacePlayerTransitionRequest(PLAYER, "not-a-record")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ParticipantTransitionRequest"):
        transition_participant("not-a-request")  # type: ignore[arg-type]


def test_values_are_immutable_slotted_and_decisions_are_deterministic() -> None:
    request = AssignCharacterTransitionRequest(PLAYER, "character-1")
    values = (
        PLAYER,
        request,
        transition_participant(request),
        transition_participant(ReplacePlayerTransitionRequest(PLAYER, SECOND_PLAYER)),
    )

    for value in values:
        assert is_dataclass(value)
        assert hasattr(type(value), "__slots__")
        with pytest.raises((AttributeError, TypeError)):
            setattr(value, fields(value)[0].name, "mutated")

    first: ParticipantTransitionDecision = transition_participant(request)
    second = transition_participant(request)
    assert first == second
    assert isinstance(
        first,
        AssignCharacterTransitionAccepted
        | AssignCharacterTransitionRejected
        | ReplacePlayerTransitionAccepted
        | ReplacePlayerTransitionRejected,
    )


def test_public_exports_are_closed_and_transition_has_no_runtime_dependencies() -> None:
    import game_runtime.session_control as session_control

    exported = {
        "AssignCharacterTransitionAccepted",
        "AssignCharacterTransitionRejected",
        "AssignCharacterTransitionRequest",
        "ParticipantTransitionContractError",
        "ParticipantTransitionContractFailureReason",
        "ParticipantTransitionDecision",
        "ParticipantTransitionEffect",
        "ParticipantTransitionRejectReason",
        "ParticipantTransitionRecordState",
        "ParticipantTransitionRequest",
        "ReplacePlayerTransitionAccepted",
        "ReplacePlayerTransitionRejected",
        "ReplacePlayerTransitionRequest",
        "transition_participant",
    }
    assert exported <= set(session_control.__all__)
    assert all(getattr(session_control, name) is globals()[name] for name in exported)
    assert not {"Accepted", "Decision", "RejectReason", "Rejected", "Request", "State"} & set(session_control.__all__)

    source = Path("game_runtime/session_control/participant_transition.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    imported_modules = {
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
        for name in (node.module,)
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    forbidden = ("actor", "clock", "random", "repository", "persistence", "io", "uuid")
    assert not any(token in module.lower() for module in imported_modules for token in forbidden)
    assert not [
        node
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and isinstance(node.value, (ast.Dict, ast.List, ast.Set))
    ]
