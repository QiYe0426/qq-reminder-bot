from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timezone

import pytest

from game_runtime.event import (
    EventVisibility,
    GameEvent,
    GameEventSource,
    GameEventType,
    ScriptSetPayload,
)
from game_runtime.participant import ParticipantMembershipState, ParticipantType
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control import (
    CanonicalControlCommandIntent,
    CandidateSessionSnapshot,
    CharacterAvailabilityStatus,
    ControlApplyBuildContext,
    ControlApplyBuildContextError,
    ControlApplyPlan,
    ControlCharacterAssignmentEvidence,
    ControlCharacterAvailabilityEvidence,
    ControlEventDeliveryEnvelope,
    ControlOperationClaim,
    ControlOperationStatus,
    ControlOwnershipBuildEvidence,
    ControlParticipantBuildView,
    ControlPlayerReplacementEvidence,
    ControlResultEventSeed,
    ControlScriptApplyEvidence,
    ControlSessionBuildView,
    ControlSetupBuildView,
    ControlSetupParticipantApplyEvidence,
    ControlSetupParticipantEvidenceError,
    DMCommandEventPayload,
    AssignCharacterPayload,
    OwnershipIntent,
    OwnershipIntentType,
    ParticipantMutation,
    ParticipantMutationType,
    ReplacePlayerPayload,
    SessionCommandType,
    SetScriptPayload,
    SetupMutation,
    SetupMutationType,
    SetupParticipantEvidenceStatus,
    fingerprint_payload,
)


NOW = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
SCOPE = {
    "game_id": "game-1",
    "session_id": "session-1",
    "observed_state_version": 4,
}


def _script(
    *,
    status: SetupParticipantEvidenceStatus = SetupParticipantEvidenceStatus.VERIFIED,
) -> ControlScriptApplyEvidence:
    return ControlScriptApplyEvidence(
        contract_version=1,
        **SCOPE,
        validation_status=status,
        script_id="script-2",
        manifest_reference="manifest-2",
        manifest_version=3,
        expected_setup_version=2,
        resulting_setup_version=3,
        ownership_evidence_reference="ownership-none-4",
        ownership_generation=None,
        visibility_boundary_reference="visibility-public-metadata-1",
    )


def _assignment(
    *,
    status: SetupParticipantEvidenceStatus = SetupParticipantEvidenceStatus.VERIFIED,
) -> ControlCharacterAssignmentEvidence:
    availability = ControlCharacterAvailabilityEvidence(
        contract_version=1,
        game_id="game-1",
        session_id="session-1",
        observed_state_version=4,
        character_id="character-1",
        target_participant_reference="participant-player-1",
        assignment_set_reference="assignments-4",
        assignment_set_version=4,
        availability_status=CharacterAvailabilityStatus.AVAILABLE,
        occupying_participant_reference=None,
    )
    return ControlCharacterAssignmentEvidence(
        contract_version=1,
        **SCOPE,
        validation_status=status,
        participant_id="participant-player-1",
        participant_reference="participant-player-1",
        character_id="character-1",
        character_binding_reference="character-binding-1",
        expected_binding_version=2,
        resulting_binding_version=3,
        assignment_version=3,
        ownership_evidence_reference="ownership-none-4",
        ownership_generation=None,
        authorization_reference="authorization-1",
        availability_evidence=availability,
    )


def _replacement(
    *,
    status: SetupParticipantEvidenceStatus = SetupParticipantEvidenceStatus.VERIFIED,
) -> ControlPlayerReplacementEvidence:
    return ControlPlayerReplacementEvidence(
        contract_version=1,
        **SCOPE,
        validation_status=status,
        old_participant_id="participant-old",
        old_participant_reference="participant-old",
        new_participant_id="participant-new",
        new_participant_reference="participant-new",
        old_expected_binding_version=4,
        old_resulting_binding_version=5,
        new_expected_binding_version=7,
        new_resulting_binding_version=8,
        authorization_reference="authorization-1",
        confirmation_reference="confirmation-1",
        replacement_policy_reference="replacement-policy-1",
        character_binding_reference="character-binding-1",
        ownership_evidence_reference="ownership-2",
        ownership_generation=2,
    )


def test_evidence_values_are_frozen_and_slotted() -> None:
    values = (
        _script(),
        _assignment(),
        _replacement(),
        ControlSetupParticipantApplyEvidence(script_apply=_script()),
    )

    for value in values[:3]:
        assert not hasattr(value, "__dict__")
        with pytest.raises(FrozenInstanceError):
            value.game_id = "other-game"  # type: ignore[misc]
    bundle = values[3]
    assert not hasattr(bundle, "__dict__")
    with pytest.raises(FrozenInstanceError):
        bundle.script_apply = None  # type: ignore[misc]


def test_scope_and_actor_turn_version_are_bound() -> None:
    evidence = ControlSetupParticipantApplyEvidence(script_apply=_script())

    evidence.validate_bindings(**SCOPE)
    with pytest.raises(ControlSetupParticipantEvidenceError):
        evidence.validate_bindings(
            game_id="other-game",
            session_id="session-1",
            observed_state_version=4,
        )
    with pytest.raises(ControlSetupParticipantEvidenceError):
        evidence.validate_bindings(
            game_id="game-1",
            session_id="session-1",
            observed_state_version=5,
        )


def test_projection_versions_are_strict_single_step_cas() -> None:
    with pytest.raises(ControlSetupParticipantEvidenceError):
        replace(_script(), resulting_setup_version=4)
    with pytest.raises(ControlSetupParticipantEvidenceError):
        replace(_assignment(), resulting_binding_version=4)
    with pytest.raises(ControlSetupParticipantEvidenceError):
        replace(_assignment(), assignment_version=2)
    with pytest.raises(ControlSetupParticipantEvidenceError):
        replace(_replacement(), old_resulting_binding_version=6)
    with pytest.raises(ControlSetupParticipantEvidenceError):
        replace(_replacement(), new_resulting_binding_version=9)


def test_boolean_contract_version_is_rejected() -> None:
    with pytest.raises(ControlSetupParticipantEvidenceError):
        replace(_script(), contract_version=True)


def test_command_reference_binding_is_closed() -> None:
    script = ControlSetupParticipantApplyEvidence(script_apply=_script())
    script.validate_command_bindings(
        SessionCommandType.SET_SCRIPT,
        SetScriptPayload(
            script_id="script-2",
            public_name="Public Script",
            manifest_reference="manifest-2",
        ),
        authorization_reference="authorization-1",
        confirmation_reference="confirmation-1",
    )
    with pytest.raises(ControlSetupParticipantEvidenceError):
        script.validate_command_bindings(
            SessionCommandType.SET_SCRIPT,
            SetScriptPayload(
                script_id="script-2",
                public_name="Public Script",
                manifest_reference="manifest-other",
            ),
            authorization_reference="authorization-1",
            confirmation_reference="confirmation-1",
        )


def test_assignment_and_replacement_governance_bindings_are_closed() -> None:
    assignment = ControlSetupParticipantApplyEvidence(
        character_assignment=_assignment()
    )
    assignment_payload = AssignCharacterPayload(
        participant_id="participant-player-1",
        character_id="character-1",
        expected_binding_version=2,
    )
    assignment.validate_command_bindings(
        SessionCommandType.ASSIGN_CHARACTER,
        assignment_payload,
        authorization_reference="authorization-1",
        confirmation_reference=None,
    )
    with pytest.raises(ControlSetupParticipantEvidenceError):
        assignment.validate_command_bindings(
            SessionCommandType.ASSIGN_CHARACTER,
            assignment_payload,
            authorization_reference="authorization-other",
            confirmation_reference=None,
        )
    with pytest.raises(ControlSetupParticipantEvidenceError):
        assignment.validate_command_bindings(
            SessionCommandType.ASSIGN_CHARACTER,
            assignment_payload,
            authorization_reference="authorization-1",
            confirmation_reference=None,
            active_ownership_generation=1,
        )

    replacement = ControlSetupParticipantApplyEvidence(
        player_replacement=_replacement()
    )
    replacement_payload = ReplacePlayerPayload(
        old_participant_id="participant-old",
        new_participant_id="participant-new",
        expected_binding_version=4,
    )
    replacement.validate_command_bindings(
        SessionCommandType.REPLACE_PLAYER,
        replacement_payload,
        authorization_reference="authorization-1",
        confirmation_reference="confirmation-1",
        active_ownership_generation=2,
    )
    for authorization, confirmation, generation in (
        ("authorization-other", "confirmation-1", 2),
        ("authorization-1", "confirmation-other", 2),
        ("authorization-1", "confirmation-1", 3),
    ):
        with pytest.raises(ControlSetupParticipantEvidenceError):
            replacement.validate_command_bindings(
                SessionCommandType.REPLACE_PLAYER,
                replacement_payload,
                authorization_reference=authorization,
                confirmation_reference=confirmation,
                active_ownership_generation=generation,
            )


@pytest.mark.parametrize(
    "command_type",
    [
        SessionCommandType.SET_SCRIPT,
        SessionCommandType.ASSIGN_CHARACTER,
        SessionCommandType.REPLACE_PLAYER,
    ],
)
def test_missing_command_evidence_is_rejected(
    command_type: SessionCommandType,
) -> None:
    with pytest.raises(ControlSetupParticipantEvidenceError):
        ControlSetupParticipantApplyEvidence().validate_for_command(
            command_type,
            **SCOPE,
        )


@pytest.mark.parametrize(
    "bundle",
    [
        ControlSetupParticipantApplyEvidence(
            script_apply=_script(status=SetupParticipantEvidenceStatus.UNKNOWN)
        ),
        ControlSetupParticipantApplyEvidence(
            character_assignment=_assignment(
                status=SetupParticipantEvidenceStatus.UNKNOWN
            )
        ),
        ControlSetupParticipantApplyEvidence(
            player_replacement=_replacement(
                status=SetupParticipantEvidenceStatus.UNKNOWN
            )
        ),
    ],
)
def test_unknown_evidence_fails_closed(
    bundle: ControlSetupParticipantApplyEvidence,
) -> None:
    command_type = (
        SessionCommandType.SET_SCRIPT
        if bundle.script_apply is not None
        else SessionCommandType.ASSIGN_CHARACTER
        if bundle.character_assignment is not None
        else SessionCommandType.REPLACE_PLAYER
    )
    with pytest.raises(ControlSetupParticipantEvidenceError):
        bundle.validate_for_command(command_type, **SCOPE)


def test_evidence_contract_rejects_hidden_or_runtime_data() -> None:
    evidence_types = (
        ControlScriptApplyEvidence,
        ControlCharacterAssignmentEvidence,
        ControlPlayerReplacementEvidence,
        ControlSetupParticipantApplyEvidence,
    )
    forbidden = (
        "script_body",
        "hidden_truth",
        "private_knowledge",
        "repository",
        "port",
        "callback",
        "task",
        "actor",
        "state_writer",
    )

    for evidence_type in evidence_types:
        field_contract = " ".join(
            f"{item.name} {item.type}" for item in fields(evidence_type)
        ).lower()
        assert not any(term in field_contract for term in forbidden)

    with pytest.raises(TypeError):
        ControlScriptApplyEvidence(  # type: ignore[call-arg]
            contract_version=1,
            **SCOPE,
            validation_status=SetupParticipantEvidenceStatus.VERIFIED,
            script_id="script-2",
            manifest_reference="manifest-2",
            manifest_version=3,
            expected_setup_version=2,
            resulting_setup_version=3,
            ownership_evidence_reference="ownership-none-4",
            ownership_generation=None,
            visibility_boundary_reference="visibility-public-metadata-1",
            script_body="secret",
        )


def test_replace_mutation_requires_and_preserves_bilateral_versions() -> None:
    mutation = ParticipantMutation(
        mutation_type=ParticipantMutationType.REPLACE,
        participant_id="participant-old",
        expected_binding_version=4,
        resulting_binding_version=8,
        replacement_participant_id="participant-new",
        old_expected_binding_version=4,
        old_resulting_binding_version=5,
        new_expected_binding_version=7,
        new_resulting_binding_version=8,
        old_character_binding_reference="character-binding-1",
        new_character_binding_reference="character-binding-1",
    )

    assert mutation.old_resulting_binding_version == 5
    assert mutation.new_resulting_binding_version == 8
    with pytest.raises(ValueError):
        replace(mutation, new_resulting_binding_version=9)


def _set_script_context(
    evidence: ControlSetupParticipantApplyEvidence,
) -> ControlApplyBuildContext:
    command_payload = SetScriptPayload(
        script_id="script-2",
        public_name="Public Script",
        manifest_reference="manifest-2",
    )
    payload_fingerprint = fingerprint_payload(command_payload)
    event_payload = DMCommandEventPayload(
        command_id="command-1",
        command_type=SessionCommandType.SET_SCRIPT,
        requester="participant-dm",
        causation_event_id="request-event-1",
        observed_state_version=4,
        payload_reference=f"sha256:{payload_fingerprint}",
        payload_fingerprint=payload_fingerprint,
    )
    event = GameEvent(
        event_id="event-7",
        game_id="game-1",
        session_id="session-1",
        event_type=GameEventType.DM_COMMAND,
        actor="participant-dm",
        source=GameEventSource.CONTROL,
        correlation_id="correlation-1",
        timestamp=NOW,
        payload=event_payload.to_mapping(),
        visibility=EventVisibility.DM_CONTROL,
        observed_state_version=4,
        causation_event_id="request-event-1",
    )
    envelope = ControlEventDeliveryEnvelope(
        event=event,
        event_sequence_no=7,
        operation_id="operation-1",
        command_id="command-1",
        observed_state_version=4,
        requester_principal_ref="participant-dm",
        requester_binding_version=2,
        authorization_reference="authorization-1",
        confirmation_reference="confirmation-1",
        correlation_id="correlation-1",
        stored_event_reference="event-7",
    )
    return ControlApplyBuildContext(
        envelope=envelope,
        claim=ControlOperationClaim(
            game_id="game-1",
            session_id="session-1",
            command_id="command-1",
            operation_id="operation-1",
            input_event_id="event-7",
            claim_id="claim-1",
            claimed_at=NOW,
        ),
        command_intent=CanonicalControlCommandIntent(
            intent_schema_version=1,
            command_type=SessionCommandType.SET_SCRIPT,
            payload=command_payload,
            payload_fingerprint=payload_fingerprint,
        ),
        session_view=ControlSessionBuildView(
            game_id="game-1",
            session_id="session-1",
            group_id="group-1",
            dm_participant_id="participant-dm",
            status=GameSessionStatus.CREATED,
            current_phase=GamePhase.LOBBY,
            state_version=4,
            last_applied_sequence_no=6,
        ),
        participant_views=(
            ControlParticipantBuildView(
                game_id="game-1",
                session_id="session-1",
                participant_id="participant-dm",
                participant_type=ParticipantType.DM,
                membership_state=ParticipantMembershipState.ACTIVE,
                character_id=None,
                binding_version=2,
            ),
        ),
        setup_view=ControlSetupBuildView(
            game_id="game-1",
            session_id="session-1",
            script_id="script-1",
            public_name="Old Public Script",
            manifest_reference="manifest-1",
            setup_version=2,
        ),
        ownership_evidence=ControlOwnershipBuildEvidence(
            game_id="game-1",
            session_id="session-1",
            group_id="group-1",
            active_generation=None,
            last_allocated_generation=0,
            observed_state_version=4,
        ),
        result_event_seed=ControlResultEventSeed(
            seed_contract_version=1,
            game_id="game-1",
            session_id="session-1",
            command_id="command-1",
            operation_id="operation-1",
            input_event_id="event-7",
            timestamp=NOW,
            correlation_id="correlation-1",
        ),
        setup_participant_evidence=evidence,
    )


def test_build_context_validates_setup_participant_reference_binding() -> None:
    _set_script_context(
        ControlSetupParticipantApplyEvidence(script_apply=_script())
    )

    with pytest.raises(ControlApplyBuildContextError):
        _set_script_context(
            ControlSetupParticipantApplyEvidence(
                script_apply=replace(_script(), manifest_reference="manifest-other")
            )
        )


def test_build_context_preserves_missing_and_unknown_for_typed_noncommit() -> None:
    missing = _set_script_context(ControlSetupParticipantApplyEvidence())
    unknown = _set_script_context(
        ControlSetupParticipantApplyEvidence(
            script_apply=_script(status=SetupParticipantEvidenceStatus.UNKNOWN)
        )
    )

    assert missing.setup_participant_evidence.script_apply is None
    assert (
        unknown.setup_participant_evidence.script_apply.validation_status
        is SetupParticipantEvidenceStatus.UNKNOWN
    )


def _set_script_plan() -> ControlApplyPlan:
    evidence = ControlSetupParticipantApplyEvidence(script_apply=_script())
    result = GameEvent(
        event_id="event-script-set-1",
        game_id="game-1",
        session_id="session-1",
        event_type=GameEventType.SCRIPT_SET,
        actor="session-actor",
        source=GameEventSource.CONTROL,
        correlation_id="correlation-1",
        timestamp=NOW,
        payload=ScriptSetPayload(
            command_id="command-1",
            operation_id="operation-1",
            input_event_id="event-7",
            result_code="SET_SCRIPT_APPLIED",
            result_state_version=5,
            script_id="script-2",
            public_name="Public Script",
            manifest_reference="manifest-2",
        ).to_mapping(),
        visibility=EventVisibility.DM_CONTROL,
        observed_state_version=4,
        causation_event_id="event-7",
    )
    return ControlApplyPlan(
        game_id="game-1",
        session_id="session-1",
        group_id="group-1",
        command_id="command-1",
        command_type=SessionCommandType.SET_SCRIPT,
        operation_id="operation-1",
        operation_claim_id="claim-1",
        input_event_id="event-7",
        input_sequence_no=7,
        expected_state_version=4,
        expected_cursor=6,
        expected_binding_version=2,
        candidate_snapshot=CandidateSessionSnapshot(
            game_id="game-1",
            session_id="session-1",
            group_id="group-1",
            dm_participant_id="participant-dm",
            status=GameSessionStatus.CREATED,
            current_phase=GamePhase.LOBBY,
            state_version=5,
            last_applied_sequence_no=7,
        ),
        participant_mutations=(),
        setup_mutations=(
            SetupMutation(
                mutation_type=SetupMutationType.SET_SCRIPT,
                script_id="script-2",
                public_name="Public Script",
                manifest_reference="manifest-2",
                manifest_version=3,
                expected_setup_version=2,
                resulting_setup_version=3,
            ),
        ),
        ownership_intent=OwnershipIntent(
            intent_type=OwnershipIntentType.UNCHANGED,
            expected_generation=None,
            resulting_generation=None,
        ),
        result_events=(result,),
        operation_terminal_state=ControlOperationStatus.SUCCESS,
        setup_participant_evidence=evidence,
    )


def test_apply_plan_binds_setup_mutation_to_evidence() -> None:
    plan = _set_script_plan()

    assert plan.setup_mutations[0].expected_setup_version == 2
    with pytest.raises(ValueError):
        replace(
            plan,
            setup_mutations=(
                replace(plan.setup_mutations[0], manifest_reference="manifest-other"),
            ),
        )
