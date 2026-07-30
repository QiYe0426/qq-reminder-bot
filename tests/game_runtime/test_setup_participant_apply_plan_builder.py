from __future__ import annotations

import ast
import importlib
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from game_runtime.event import (
    EventVisibility,
    GameEvent,
    GameEventSource,
    GameEventType,
    validate_control_result_event,
)
from game_runtime.participant import ParticipantMembershipState, ParticipantType
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control import (
    AssignCharacterPayload,
    AssignCharacterTransitionRequest,
    BuildNonCommit,
    BuildPlanReady,
    BuildReject,
    CanonicalControlCommandIntent,
    CharacterAvailabilityStatus,
    ControlApplyBuildContext,
    ControlCharacterAssignmentEvidence,
    ControlCharacterAvailabilityEvidence,
    ControlEventDeliveryEnvelope,
    ControlOperationClaim,
    ControlOwnershipBuildEvidence,
    ControlParticipantBuildView,
    ControlPlayerReplacementEvidence,
    ControlResultEventSeed,
    ControlScriptApplyEvidence,
    ControlSessionBuildView,
    ControlSetupBuildView,
    ControlSetupParticipantApplyEvidence,
    DMCommandEventPayload,
    ParticipantMutationType,
    ReplacePlayerPayload,
    ReplacePlayerTransitionRequest,
    SessionCommandType,
    SetScriptPayload,
    SetupMutationType,
    SetupParticipantControlApplyPlanBuilder,
    SetupParticipantEvidenceStatus,
    SetupParticipantRejectReason,
    fingerprint_payload,
)


NOW = datetime(2026, 7, 17, 11, 0, tzinfo=timezone.utc)


def _context(
    command_type: SessionCommandType,
    *,
    status: GameSessionStatus = GameSessionStatus.CREATED,
    setup_view: ControlSetupBuildView | None = None,
    availability: CharacterAvailabilityStatus = CharacterAvailabilityStatus.AVAILABLE,
    old_character: str | None = "character-1",
    new_character: str | None = None,
    evidence_status: SetupParticipantEvidenceStatus = SetupParticipantEvidenceStatus.VERIFIED,
) -> ControlApplyBuildContext:
    payload = {
        SessionCommandType.SET_SCRIPT: SetScriptPayload(
            script_id="script-2", public_name="Public Script 2", manifest_reference="manifest-2"
        ),
        SessionCommandType.ASSIGN_CHARACTER: AssignCharacterPayload(
            participant_id="player-old", character_id="character-1", expected_binding_version=2
        ),
        SessionCommandType.REPLACE_PLAYER: ReplacePlayerPayload(
            old_participant_id="player-old", new_participant_id="player-new", expected_binding_version=2
        ),
    }[command_type]
    fingerprint = fingerprint_payload(payload)
    event_payload = DMCommandEventPayload(
        command_id="command-1", command_type=command_type, requester="dm-1",
        causation_event_id="request-1", observed_state_version=4,
        payload_reference=f"sha256:{fingerprint}", payload_fingerprint=fingerprint,
    )
    event = GameEvent(
        event_id="event-7", game_id="game-1", session_id="session-1",
        event_type=GameEventType.DM_COMMAND, actor="dm-1", source=GameEventSource.CONTROL,
        correlation_id="correlation-1", timestamp=NOW, payload=event_payload.to_mapping(),
        visibility=EventVisibility.DM_CONTROL, observed_state_version=4,
        causation_event_id="request-1",
    )
    envelope = ControlEventDeliveryEnvelope(
        event=event, event_sequence_no=7, operation_id="operation-1", command_id="command-1",
        observed_state_version=4, requester_principal_ref="dm-1", requester_binding_version=1,
        authorization_reference="authorization-1", confirmation_reference="confirmation-1",
        correlation_id="correlation-1", stored_event_reference="event-7",
    )
    participants = (
        ControlParticipantBuildView(
            game_id="game-1", session_id="session-1", participant_id="dm-1",
            participant_type=ParticipantType.DM,
            membership_state=ParticipantMembershipState.ACTIVE,
            character_id=None, binding_version=1,
        ),
        ControlParticipantBuildView(
            game_id="game-1", session_id="session-1", participant_id="player-old",
            participant_type=ParticipantType.PLAYER,
            membership_state=ParticipantMembershipState.ACTIVE,
            character_id=old_character if command_type is SessionCommandType.REPLACE_PLAYER else None,
            binding_version=2,
        ),
        ControlParticipantBuildView(
            game_id="game-1", session_id="session-1", participant_id="player-new",
            participant_type=ParticipantType.PLAYER,
            membership_state=ParticipantMembershipState.ACTIVE,
            character_id=new_character, binding_version=5,
        ),
    )
    ownership_generation = None if status is GameSessionStatus.CREATED else 3
    common = dict(
        contract_version=1, game_id="game-1", session_id="session-1",
        observed_state_version=4, validation_status=evidence_status,
        ownership_evidence_reference="ownership-1", ownership_generation=ownership_generation,
    )
    evidence: ControlSetupParticipantApplyEvidence
    if command_type is SessionCommandType.SET_SCRIPT:
        evidence = ControlSetupParticipantApplyEvidence(
            script_apply=ControlScriptApplyEvidence(
                **common, script_id="script-2", manifest_reference="manifest-2", manifest_version=3,
                expected_setup_version=0 if setup_view is None else setup_view.setup_version,
                resulting_setup_version=1 if setup_view is None else setup_view.setup_version + 1,
                visibility_boundary_reference="visibility-public-1",
            )
        )
    elif command_type is SessionCommandType.ASSIGN_CHARACTER:
        occupant = {
            CharacterAvailabilityStatus.AVAILABLE: None,
            CharacterAvailabilityStatus.ASSIGNED_TO_TARGET: "player-old",
            CharacterAvailabilityStatus.ASSIGNED_TO_OTHER: "player-new",
            CharacterAvailabilityStatus.UNKNOWN: None,
        }[availability]
        availability_evidence = ControlCharacterAvailabilityEvidence(
            contract_version=1, game_id="game-1", session_id="session-1",
            observed_state_version=4, character_id="character-1",
            target_participant_reference="player-old", assignment_set_reference="assignments-4",
            assignment_set_version=4, availability_status=availability,
            occupying_participant_reference=occupant,
        )
        evidence = ControlSetupParticipantApplyEvidence(
            character_assignment=ControlCharacterAssignmentEvidence(
                **common, participant_id="player-old", participant_reference="player-old",
                character_id="character-1", character_binding_reference="binding-character-1",
                expected_binding_version=2, resulting_binding_version=3, assignment_version=3,
                authorization_reference="authorization-1", availability_evidence=availability_evidence,
            )
        )
    else:
        binding_ref = "binding-character-1" if old_character is not None else None
        evidence = ControlSetupParticipantApplyEvidence(
            player_replacement=ControlPlayerReplacementEvidence(
                **common, old_participant_id="player-old", old_participant_reference="player-old",
                new_participant_id="player-new", new_participant_reference="player-new",
                old_expected_binding_version=2, old_resulting_binding_version=3,
                new_expected_binding_version=5, new_resulting_binding_version=6,
                authorization_reference="authorization-1", confirmation_reference="confirmation-1",
                replacement_policy_reference="replacement-policy-1",
                character_binding_reference=binding_ref,
            )
        )
    return ControlApplyBuildContext(
        envelope=envelope,
        claim=ControlOperationClaim(
            game_id="game-1", session_id="session-1", command_id="command-1",
            operation_id="operation-1", input_event_id="event-7", claim_id="claim-1", claimed_at=NOW,
        ),
        command_intent=CanonicalControlCommandIntent(
            intent_schema_version=1, command_type=command_type, payload=payload,
            payload_fingerprint=fingerprint,
        ),
        session_view=ControlSessionBuildView(
            game_id="game-1", session_id="session-1", group_id="group-1",
            dm_participant_id="dm-1", status=status, current_phase=GamePhase.LOBBY,
            state_version=4, last_applied_sequence_no=6,
        ),
        participant_views=participants, setup_view=setup_view,
        ownership_evidence=ControlOwnershipBuildEvidence(
            game_id="game-1", session_id="session-1", group_id="group-1",
            active_generation=ownership_generation, last_allocated_generation=3,
            observed_state_version=4,
        ),
        result_event_seed=ControlResultEventSeed(
            seed_contract_version=1, game_id="game-1", session_id="session-1",
            command_id="command-1", operation_id="operation-1", input_event_id="event-7",
            timestamp=NOW, correlation_id="correlation-1",
        ),
        setup_participant_evidence=evidence,
    )


def test_set_script_builds_schema_v1_setup_plan() -> None:
    outcome = SetupParticipantControlApplyPlanBuilder().build(_context(SessionCommandType.SET_SCRIPT))
    assert isinstance(outcome, BuildPlanReady)
    mutation = outcome.plan.setup_mutations[0]
    assert mutation.mutation_type is SetupMutationType.SET_SCRIPT
    assert mutation.manifest_version == 3
    assert outcome.plan.result_events[0].event_type is GameEventType.SCRIPT_SET
    assert "setup_version" not in outcome.plan.result_events[0].payload


def test_set_script_transition_delegation_preserves_legacy_confirmation_and_rejection() -> None:
    builder = SetupParticipantControlApplyPlanBuilder()
    initialized_without_confirmation = builder.build(
        replace(
            _context(SessionCommandType.SET_SCRIPT),
            envelope=replace(
                _context(SessionCommandType.SET_SCRIPT).envelope,
                confirmation_reference=None,
            ),
        )
    )
    assert isinstance(initialized_without_confirmation, BuildPlanReady)

    duplicate_setup = ControlSetupBuildView(
        game_id="game-1", session_id="session-1", script_id="script-2",
        public_name="Public Script 2", manifest_reference="manifest-2", setup_version=2,
    )
    duplicate = builder.build(
        _context(SessionCommandType.SET_SCRIPT, setup_view=duplicate_setup)
    )
    assert isinstance(duplicate, BuildReject)
    assert (
        duplicate.plan.rejection_event.payload["reason_code"]
        == SetupParticipantRejectReason.SCRIPT_ALREADY_SET.value
    )

    replacement_setup = replace(
        duplicate_setup, script_id="script-1", manifest_reference="manifest-1"
    )
    replacement_context = _context(
        SessionCommandType.SET_SCRIPT, setup_view=replacement_setup
    )
    replacement_without_confirmation = builder.build(
        replace(
            replacement_context,
            envelope=replace(
                replacement_context.envelope, confirmation_reference=None
            ),
        )
    )
    assert isinstance(replacement_without_confirmation, BuildReject)
    assert (
        replacement_without_confirmation.plan.rejection_event.payload["reason_code"]
        == SetupParticipantRejectReason.SCRIPT_REPLACEMENT_NOT_CONFIRMED.value
    )


def test_assign_character_builds_bound_participant_mutation() -> None:
    outcome = SetupParticipantControlApplyPlanBuilder().build(_context(SessionCommandType.ASSIGN_CHARACTER))
    assert isinstance(outcome, BuildPlanReady)
    mutation = outcome.plan.participant_mutations[0]
    assert mutation.mutation_type is ParticipantMutationType.ASSIGN_CHARACTER
    assert mutation.character_binding_reference == "binding-character-1"
    assert validate_control_result_event(outcome.plan.result_events[0]).binding_version == 3


def test_participant_record_transitions_delegate_and_preserve_legacy_results(
    monkeypatch,
) -> None:
    builder_module = importlib.import_module(
        "game_runtime.session_control.setup_participant_builder"
    )
    original = builder_module.transition_participant
    requests = []

    def tracked_transition(request):
        requests.append(request)
        return original(request)

    monkeypatch.setattr(builder_module, "transition_participant", tracked_transition)
    builder = SetupParticipantControlApplyPlanBuilder()

    assert isinstance(
        builder.build(_context(SessionCommandType.ASSIGN_CHARACTER)), BuildPlanReady
    )
    assert isinstance(
        builder.build(_context(SessionCommandType.REPLACE_PLAYER)), BuildPlanReady
    )
    assert isinstance(
        builder.build(
            _context(
                SessionCommandType.ASSIGN_CHARACTER,
                availability=CharacterAvailabilityStatus.ASSIGNED_TO_TARGET,
            )
        ),
        BuildReject,
    )
    conflicting_context = _context(SessionCommandType.ASSIGN_CHARACTER)
    conflicting_context = replace(
        conflicting_context,
        participant_views=tuple(
            replace(view, character_id="character-2")
            if view.participant_id == "player-old"
            else view
            for view in conflicting_context.participant_views
        ),
    )
    conflicting = builder.build(conflicting_context)
    assert isinstance(conflicting, BuildReject)
    assert (
        conflicting.plan.rejection_event.payload["reason_code"]
        == SetupParticipantRejectReason.CHARACTER_CONFLICT.value
    )
    assert [type(request) for request in requests] == [
        AssignCharacterTransitionRequest,
        ReplacePlayerTransitionRequest,
        AssignCharacterTransitionRequest,
    ]


def test_legacy_available_assign_maps_same_existing_character_to_conflict_and_delegates(
    monkeypatch,
) -> None:
    builder_module = importlib.import_module(
        "game_runtime.session_control.setup_participant_builder"
    )
    original = builder_module.transition_participant
    requests = []

    def tracked_transition(request):
        requests.append(request)
        return original(request)

    monkeypatch.setattr(builder_module, "transition_participant", tracked_transition)
    context = _context(
        SessionCommandType.ASSIGN_CHARACTER,
        availability=CharacterAvailabilityStatus.AVAILABLE,
    )
    context = replace(
        context,
        participant_views=tuple(
            replace(view, character_id="character-1")
            if view.participant_id == "player-old"
            else view
            for view in context.participant_views
        ),
    )

    outcome = SetupParticipantControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildReject)
    assert (
        outcome.plan.rejection_event.payload["reason_code"]
        == SetupParticipantRejectReason.CHARACTER_CONFLICT.value
    )
    assert len(requests) == 1
    assert isinstance(requests[0], AssignCharacterTransitionRequest)


def test_replace_player_transfers_character_reference_atomically() -> None:
    outcome = SetupParticipantControlApplyPlanBuilder().build(_context(SessionCommandType.REPLACE_PLAYER))
    assert isinstance(outcome, BuildPlanReady)
    mutation = outcome.plan.participant_mutations[0]
    assert mutation.old_character_binding_reference == "binding-character-1"
    assert mutation.new_character_binding_reference == "binding-character-1"
    assert outcome.plan.result_events[0].event_type is GameEventType.PLAYER_REPLACED


def test_invalid_lifecycle_character_conflict_and_replacement_are_rejects() -> None:
    builder = SetupParticipantControlApplyPlanBuilder()
    assert isinstance(builder.build(_context(SessionCommandType.SET_SCRIPT, status=GameSessionStatus.RUNNING)), BuildReject)
    assert isinstance(builder.build(_context(SessionCommandType.ASSIGN_CHARACTER, availability=CharacterAvailabilityStatus.ASSIGNED_TO_OTHER)), BuildReject)
    assert isinstance(builder.build(_context(SessionCommandType.REPLACE_PLAYER, new_character="character-2")), BuildReject)


def test_missing_unknown_and_version_mismatch_are_noncommit() -> None:
    builder = SetupParticipantControlApplyPlanBuilder()
    context = _context(SessionCommandType.SET_SCRIPT)
    assert isinstance(builder.build(replace(context, setup_participant_evidence=ControlSetupParticipantApplyEvidence())), BuildNonCommit)
    assert isinstance(builder.build(_context(SessionCommandType.ASSIGN_CHARACTER, availability=CharacterAvailabilityStatus.UNKNOWN)), BuildNonCommit)
    assert isinstance(
        builder.build(
            _context(SessionCommandType.SET_SCRIPT, status=GameSessionStatus.PAUSED)
        ),
        BuildNonCommit,
    )
    script = context.setup_participant_evidence.script_apply
    assert script is not None
    stale_context = replace(
        context,
        session_view=replace(context.session_view, state_version=5),
        ownership_evidence=replace(
            context.ownership_evidence, observed_state_version=5
        ),
        setup_participant_evidence=ControlSetupParticipantApplyEvidence(
            script_apply=replace(script, observed_state_version=5)
        ),
    )
    assert isinstance(builder.build(stale_context), BuildNonCommit)


def test_same_context_produces_equal_plan_and_event_identity() -> None:
    context = _context(SessionCommandType.REPLACE_PLAYER)
    first = SetupParticipantControlApplyPlanBuilder().build(context)
    second = SetupParticipantControlApplyPlanBuilder().build(context)
    assert first == second
    assert isinstance(first, BuildPlanReady) and isinstance(second, BuildPlanReady)
    assert first.plan.result_events[0].event_id == second.plan.result_events[0].event_id


def test_builder_imports_have_no_runtime_or_io_dependencies() -> None:
    source = Path("game_runtime/session_control/setup_participant_builder.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules = {
        name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for name in ([node.module] if isinstance(node, ast.ImportFrom) else [])
        if name is not None
    }
    modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    forbidden = ("repository", "persistence", "ports", "actor", "coordinator", "recovery", "sqlite")
    assert not any(token in module.lower() for module in modules for token in forbidden)
