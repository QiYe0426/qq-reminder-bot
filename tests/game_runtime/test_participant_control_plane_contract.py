from __future__ import annotations

import ast
from dataclasses import replace
from copy import deepcopy
import inspect
from pathlib import Path

import pytest

import game_runtime.session_control.participant_control_builder as builder_module
from game_runtime.event import (
    CharacterAssignedPayload,
    GameEventType,
    PlayerReplacedPayload,
    SessionControlRejectedPayload,
    validate_control_result_event,
)
from game_runtime.participant import ParticipantMembershipState, ParticipantType
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control import (
    BuildNonCommit,
    BuildNonCommitReason,
    BuildPlanReady,
    BuildReject,
    CandidateGameSnapshot,
    CharacterAvailabilityStatus,
    ControlSetupParticipantApplyEvidence,
    GameRuleSnapshotSlice,
    HiddenGameStateSlice,
    LifecycleSnapshotSlice,
    OwnershipIntentType,
    OwnershipIntent,
    ParticipantMutationType,
    ParticipantSnapshotRecord,
    ParticipantSnapshotSlice,
    PhaseSnapshotSlice,
    QuestSnapshotSlice,
    SessionCommandType,
    SetupParticipantEvidenceStatus,
    SetupSnapshotSlice,
)
from game_runtime.session_control.participant_control_builder import (
    ParticipantControlApplyPlanBuilder,
    ParticipantControlRejectReason,
)
from game_runtime.session_control.participant_transition import (
    AssignCharacterTransitionAccepted,
    AssignCharacterTransitionRequest,
    ReplacePlayerTransitionAccepted,
    ReplacePlayerTransitionRequest,
    ParticipantTransitionRecordState,
    ParticipantTransitionEffect,
    transition_participant,
)
from test_setup_participant_apply_plan_builder import _context as legacy_context


def _context(
    command_type: SessionCommandType = SessionCommandType.ASSIGN_CHARACTER,
    *,
    status: GameSessionStatus = GameSessionStatus.CREATED,
    phase: GamePhase = GamePhase.LOBBY,
    old_character: str | None = "character-1",
    availability: CharacterAvailabilityStatus = CharacterAvailabilityStatus.AVAILABLE,
    evidence_status: SetupParticipantEvidenceStatus = SetupParticipantEvidenceStatus.VERIFIED,
    snapshot_cursor: int = 6,
):
    context = legacy_context(
        command_type,
        status=status,
        old_character=old_character,
        availability=availability,
        evidence_status=evidence_status,
    )
    context = replace(
        context,
        session_view=replace(
            context.session_view,
            status=status,
            current_phase=phase,
        ),
    )
    records = tuple(
        ParticipantSnapshotRecord(
            participant_id=view.participant_id,
            participant_type=view.participant_type,
            membership_state=view.membership_state,
            character_id=view.character_id,
            binding_version=view.binding_version,
        )
        for view in sorted(context.participant_views, key=lambda view: view.participant_id)
    )
    snapshot = CandidateGameSnapshot(
        game_id="game-1",
        session_id="session-1",
        group_id="group-1",
        dm_participant_id="dm-1",
        status=status,
        current_phase=phase,
        state_version=4,
        last_applied_sequence_no=snapshot_cursor,
        snapshot_schema_version=3,
        lifecycle=LifecycleSnapshotSlice(1, 1, status),
        phase=PhaseSnapshotSlice(1, 1, phase),
        setup=SetupSnapshotSlice(1, 0, None, None, None, None),
        participants=ParticipantSnapshotSlice(1, 1, records),
        game_rules=GameRuleSnapshotSlice(2, 0, None, None),
        quest=QuestSnapshotSlice(1, 0, None, None, None),
        hidden_state=HiddenGameStateSlice(1, 0, None),
    )
    evidence = context.setup_participant_evidence
    assignment = evidence.character_assignment
    replacement = evidence.player_replacement
    if assignment is not None:
        assignment = replace(
            assignment,
            ownership_generation=context.ownership_evidence.active_generation,
        )
    if replacement is not None:
        replacement = replace(
            replacement,
            ownership_generation=context.ownership_evidence.active_generation,
        )
    return replace(
        context,
        session_view=replace(context.session_view, current_game_snapshot=snapshot),
        setup_participant_evidence=ControlSetupParticipantApplyEvidence(
            character_assignment=assignment,
            player_replacement=replacement,
        ),
    )


def _with_third_player(
    context,
    *,
    character_id: str | None,
):
    template = next(
        view for view in context.participant_views if view.participant_id == "player-new"
    )
    participant_views = context.participant_views + (
        replace(
            template,
            participant_id="player-third",
            character_id=character_id,
            binding_version=8,
        ),
    )
    current = context.session_view.current_game_snapshot
    assert current is not None
    records = tuple(
        ParticipantSnapshotRecord(
            participant_id=view.participant_id,
            participant_type=view.participant_type,
            membership_state=view.membership_state,
            character_id=view.character_id,
            binding_version=view.binding_version,
        )
        for view in sorted(participant_views, key=lambda view: view.participant_id)
    )
    return replace(
        context,
        participant_views=participant_views,
        session_view=replace(
            context.session_view,
            current_game_snapshot=replace(
                current,
                participants=ParticipantSnapshotSlice(1, 1, records),
            ),
        ),
    )


@pytest.mark.parametrize("status", (GameSessionStatus.CREATED, GameSessionStatus.PAUSED))
def test_assign_builds_complete_composite_plan_and_only_replaces_target_record(status):
    context = _context(status=status)
    current = context.session_view.current_game_snapshot
    assert current is not None

    outcome = ParticipantControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildPlanReady)
    plan = outcome.plan
    candidate = plan.candidate_snapshot
    assert isinstance(candidate, CandidateGameSnapshot)
    assert candidate.state_version == current.state_version + 1
    assert candidate.last_applied_sequence_no == context.envelope.event_sequence_no
    assert candidate.lifecycle is current.lifecycle
    assert candidate.phase is current.phase
    assert candidate.setup is current.setup
    assert candidate.game_rules is current.game_rules
    assert candidate.hidden_state is current.hidden_state
    assert candidate.participants is not current.participants
    assert candidate.participants.domain_version == current.participants.domain_version + 1
    assert tuple(record.participant_id for record in candidate.participants.participants) == tuple(sorted(record.participant_id for record in current.participants.participants))
    target = next(record for record in candidate.participants.participants if record.participant_id == "player-old")
    assert target.character_id == "character-1"
    assert target.binding_version == 3
    assert len(plan.participant_mutations) == 1
    assert plan.participant_mutations[0].mutation_type is ParticipantMutationType.ASSIGN_CHARACTER
    assert plan.setup_mutations == ()
    assert plan.ownership_intent.intent_type is (OwnershipIntentType.UNCHANGED if status is GameSessionStatus.CREATED else OwnershipIntentType.RETAIN)
    expected_generation = None if status is GameSessionStatus.CREATED else 3
    assert plan.ownership_intent.expected_generation == expected_generation
    assert plan.ownership_intent.resulting_generation == expected_generation
    assert len(plan.result_events) == 1
    assert plan.result_events[0].event_type is GameEventType.CHARACTER_ASSIGNED
    payload = validate_control_result_event(plan.result_events[0])
    assert isinstance(payload, CharacterAssignedPayload)
    assert payload.result_code == "ASSIGN_CHARACTER_APPLIED"
    assert payload.result_state_version == candidate.state_version


@pytest.mark.parametrize("status", (GameSessionStatus.CREATED, GameSessionStatus.PAUSED))
def test_replace_builds_complete_composite_plan_for_empty_or_assigned_player(status):
    context = _context(SessionCommandType.REPLACE_PLAYER, status=status)
    current = context.session_view.current_game_snapshot
    assert current is not None

    outcome = ParticipantControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildPlanReady)
    plan = outcome.plan
    candidate = plan.candidate_snapshot
    assert isinstance(candidate, CandidateGameSnapshot)
    old = next(record for record in candidate.participants.participants if record.participant_id == "player-old")
    new = next(record for record in candidate.participants.participants if record.participant_id == "player-new")
    assert old.membership_state is ParticipantMembershipState.REPLACED
    assert old.character_id is None
    assert new.membership_state is ParticipantMembershipState.ACTIVE
    assert new.character_id == "character-1"
    assert len(plan.participant_mutations) == 1
    assert plan.participant_mutations[0].mutation_type is ParticipantMutationType.REPLACE
    assert plan.setup_mutations == ()
    expected_generation = None if status is GameSessionStatus.CREATED else 3
    assert plan.ownership_intent.intent_type is (
        OwnershipIntentType.UNCHANGED if status is GameSessionStatus.CREATED else OwnershipIntentType.RETAIN
    )
    assert plan.ownership_intent.expected_generation == expected_generation
    assert plan.ownership_intent.resulting_generation == expected_generation
    payload = validate_control_result_event(plan.result_events[0])
    assert isinstance(payload, PlayerReplacedPayload)
    assert payload.result_code == "REPLACE_PLAYER_APPLIED"


def test_replace_without_a_character_transfers_no_reference():
    outcome = ParticipantControlApplyPlanBuilder().build(
        _context(SessionCommandType.REPLACE_PLAYER, old_character=None)
    )
    assert isinstance(outcome, BuildPlanReady)
    mutation = outcome.plan.participant_mutations[0]
    assert mutation.old_character_binding_reference is None
    assert mutation.new_character_binding_reference is None
    candidate = outcome.plan.candidate_snapshot
    old = next(record for record in candidate.participants.participants if record.participant_id == "player-old")
    new = next(record for record in candidate.participants.participants if record.participant_id == "player-new")
    assert old.character_id is None and new.character_id is None


def test_replace_public_builder_rejects_third_active_player_with_old_character():
    context = _with_third_player(
        _context(SessionCommandType.REPLACE_PLAYER),
        character_id="character-1",
    )

    outcome = ParticipantControlApplyPlanBuilder().build(context)

    assert outcome == BuildNonCommit(
        BuildNonCommitReason.EVIDENCE_MISMATCH,
        "REPLACEMENT_CHARACTER_BINDING_MISMATCH",
    )


def test_replace_private_composer_rejects_third_active_player_with_old_character():
    context = _with_third_player(
        _context(SessionCommandType.REPLACE_PLAYER),
        character_id="character-1",
    )
    current = context.session_view.current_game_snapshot
    evidence = context.setup_participant_evidence.player_replacement
    assert current is not None and evidence is not None
    old = next(
        view for view in context.participant_views if view.participant_id == "player-old"
    )
    new = next(
        view for view in context.participant_views if view.participant_id == "player-new"
    )
    decision = transition_participant(
        ReplacePlayerTransitionRequest(
            ParticipantTransitionRecordState(
                old.participant_id,
                old.participant_type,
                old.membership_state,
                old.character_id,
                old.binding_version,
            ),
            ParticipantTransitionRecordState(
                new.participant_id,
                new.participant_type,
                new.membership_state,
                new.character_id,
                new.binding_version,
            ),
        )
    )
    assert isinstance(decision, ReplacePlayerTransitionAccepted)

    outcome = builder_module._compose_participant_apply_plan(
        context=context,
        current=current,
        decision=decision,
        evidence=evidence,
        ownership=outcome_ownership(context),
    )

    assert outcome == BuildNonCommit(
        BuildNonCommitReason.EVIDENCE_MISMATCH,
        "REPLACEMENT_CHARACTER_BINDING_MISMATCH",
    )


def test_replace_without_old_character_does_not_transfer_third_players_character():
    context = _with_third_player(
        _context(SessionCommandType.REPLACE_PLAYER, old_character=None),
        character_id="character-1",
    )

    outcome = ParticipantControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildPlanReady)
    candidate = outcome.plan.candidate_snapshot
    new = next(
        record
        for record in candidate.participants.participants
        if record.participant_id == "player-new"
    )
    third = next(
        record
        for record in candidate.participants.participants
        if record.participant_id == "player-third"
    )
    assert new.character_id is None
    assert third.character_id == "character-1"


@pytest.mark.parametrize(
    ("command_type", "status", "phase", "reason"),
    [
        (SessionCommandType.ASSIGN_CHARACTER, GameSessionStatus.RUNNING, GamePhase.INTRODUCTION, ParticipantControlRejectReason.INVALID_LIFECYCLE),
        (SessionCommandType.REPLACE_PLAYER, GameSessionStatus.RUNNING, GamePhase.INTRODUCTION, ParticipantControlRejectReason.PLAYER_REPLACEMENT_NOT_ALLOWED),
    ],
)
def test_business_rejections_remain_noncommitting(command_type, status, phase, reason):
    outcome = ParticipantControlApplyPlanBuilder().build(_context(command_type, status=status, phase=phase))
    assert isinstance(outcome, BuildReject)
    payload = validate_control_result_event(outcome.plan.rejection_event)
    assert isinstance(payload, SessionControlRejectedPayload)
    assert payload.reason_code == reason.value


@pytest.mark.parametrize(
    ("availability", "reason"),
    [
        (CharacterAvailabilityStatus.ASSIGNED_TO_TARGET, ParticipantControlRejectReason.CHARACTER_ALREADY_ASSIGNED),
        (CharacterAvailabilityStatus.ASSIGNED_TO_OTHER, ParticipantControlRejectReason.CHARACTER_CONFLICT),
    ],
)
def test_assignment_availability_business_rejections(availability, reason):
    context = legacy_context(SessionCommandType.ASSIGN_CHARACTER, availability=availability)
    occupied_id = "player-old" if availability is CharacterAvailabilityStatus.ASSIGNED_TO_TARGET else "player-new"
    participant_views = tuple(
        replace(view, character_id="character-1") if view.participant_id == occupied_id else view
        for view in context.participant_views
    )
    records = tuple(
        ParticipantSnapshotRecord(
            participant_id=view.participant_id,
            participant_type=view.participant_type,
            membership_state=view.membership_state,
            character_id=view.character_id,
            binding_version=view.binding_version,
        )
        for view in sorted(participant_views, key=lambda view: view.participant_id)
    )
    current = _context().session_view.current_game_snapshot
    assert current is not None
    snapshot = replace(current, participants=ParticipantSnapshotSlice(1, 1, records))
    context = replace(context, participant_views=participant_views, session_view=replace(context.session_view, current_game_snapshot=snapshot))
    outcome = ParticipantControlApplyPlanBuilder().build(context)
    assert isinstance(outcome, BuildReject)
    assert validate_control_result_event(outcome.plan.rejection_event).reason_code == reason.value


def test_unknown_availability_and_bad_composite_binding_fail_closed():
    context = _context()
    evidence = context.setup_participant_evidence.character_assignment
    assert evidence is not None
    unknown = replace(
        context,
        setup_participant_evidence=ControlSetupParticipantApplyEvidence(
            character_assignment=replace(
                evidence,
                availability_evidence=replace(
                    evidence.availability_evidence,
                    availability_status=CharacterAvailabilityStatus.UNKNOWN,
                ),
            ),
        ),
    )
    assert ParticipantControlApplyPlanBuilder().build(unknown) == BuildNonCommit(
        BuildNonCommitReason.EVIDENCE_MISMATCH, "CHARACTER_AVAILABILITY_UNKNOWN"
    )
    current = context.session_view.current_game_snapshot
    assert current is not None
    original = current.participants.participants[0]
    object.__setattr__(
        current.participants,
        "participants",
        (replace(original, binding_version=original.binding_version + 1),)
        + current.participants.participants[1:],
    )
    assert ParticipantControlApplyPlanBuilder().build(context) == BuildNonCommit(
        BuildNonCommitReason.EVIDENCE_MISMATCH,
        "COMPOSITE_PARTICIPANT_BINDING_MISMATCH",
    )


def test_builder_and_reject_reasons_are_publicly_exported():
    from game_runtime.session_control import (
        ParticipantControlApplyPlanBuilder as ExportedBuilder,
        ParticipantControlRejectReason as ExportedReason,
    )

    assert ExportedBuilder is ParticipantControlApplyPlanBuilder
    assert ExportedReason is ParticipantControlRejectReason


def test_malformed_or_unsupported_input_never_commits():
    builder = ParticipantControlApplyPlanBuilder()
    assert builder.build(object()) == BuildNonCommit(
        BuildNonCommitReason.INVALID_CONTEXT,
        "CONTROL_APPLY_CONTEXT_TYPE_INVALID",
    )
    unsupported = _context()
    object.__setattr__(
        unsupported.command_intent,
        "command_type",
        SessionCommandType.PAUSE_GAME,
    )
    assert builder.build(unsupported) == BuildNonCommit(
        BuildNonCommitReason.REDUCER_UNAVAILABLE,
        "PAUSE_GAME_REDUCER_UNAVAILABLE",
    )
    missing = _context()
    object.__setattr__(missing.session_view, "current_game_snapshot", None)
    assert builder.build(missing) == BuildNonCommit(
        BuildNonCommitReason.EVIDENCE_MISMATCH,
        "COMPOSITE_CURRENT_SNAPSHOT_MISSING",
    )
    deleted = _context()
    object.__delattr__(deleted.command_intent, "intent_schema_version")
    assert builder.build(deleted) == BuildNonCommit(
        BuildNonCommitReason.INVALID_CONTEXT,
        "CONTROL_APPLY_CONTEXT_INVALID",
    )


def test_builder_is_sync_stateless_deterministic_and_has_no_runtime_dependencies():
    context = _context()
    before = deepcopy(context)
    builder = ParticipantControlApplyPlanBuilder()
    first, second = builder.build(context), builder.build(context)
    assert builder.__slots__ == ()
    assert not hasattr(builder, "__dict__")
    assert not inspect.iscoroutinefunction(builder.build)
    assert first == second
    assert context == before
    source = Path("game_runtime/session_control/participant_control_builder.py").read_text(encoding="utf-8")
    imports = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    forbidden = ("actor", "gate", "processor", "coordinator", "receipt", "visibility", "recovery", "persistence", "repository", "sqlite", "setup_control_builder", "phase_control_builder")
    assert not any(token in module.casefold() for module in imports for token in forbidden)


def test_private_composer_fails_closed_for_incomplete_inputs():
    outcome = builder_module._compose_participant_apply_plan(
        context=_context(),
        current=object(),  # type: ignore[arg-type]
        decision=object(),  # type: ignore[arg-type]
        evidence=object(),  # type: ignore[arg-type]
        ownership=object(),  # type: ignore[arg-type]
    )
    assert outcome == BuildNonCommit(
        BuildNonCommitReason.INVALID_CONTEXT,
        "PARTICIPANT_COMPOSITION_INVALID",
    )


def test_private_composer_rejects_non_context_without_dereferencing_it():
    outcome = builder_module._compose_participant_apply_plan(
        context=object(),  # type: ignore[arg-type]
        current=object(),  # type: ignore[arg-type]
        decision=object(),  # type: ignore[arg-type]
        evidence=object(),  # type: ignore[arg-type]
        ownership=object(),  # type: ignore[arg-type]
    )
    assert outcome == BuildNonCommit(
        BuildNonCommitReason.INVALID_CONTEXT,
        "PARTICIPANT_COMPOSITION_INVALID",
    )


@pytest.mark.parametrize(
    "tamper",
    (
        "claim_command_id",
        "intent_payload_fingerprint",
        "envelope_command_id",
        "seed_command_id",
        "seed_correlation_id",
        "seed_timestamp",
    ),
)
def test_private_composer_revalidates_consumed_context_before_composition(tamper):
    context = _context()
    current = context.session_view.current_game_snapshot
    evidence = context.setup_participant_evidence.character_assignment
    assert current is not None and evidence is not None
    view = next(
        view for view in context.participant_views if view.participant_id == "player-old"
    )
    decision = transition_participant(
        AssignCharacterTransitionRequest(
            ParticipantTransitionRecordState(
                view.participant_id,
                view.participant_type,
                view.membership_state,
                view.character_id,
                view.binding_version,
            ),
            "character-1",
        )
    )
    assert isinstance(decision, AssignCharacterTransitionAccepted)
    ownership = outcome_ownership(context)
    if tamper == "claim_command_id":
        object.__setattr__(context.claim, "command_id", "tampered-command")
    elif tamper == "intent_payload_fingerprint":
        object.__setattr__(context.command_intent, "payload_fingerprint", "0" * 64)
    elif tamper == "envelope_command_id":
        object.__setattr__(context.envelope, "command_id", "tampered-command")
    elif tamper == "seed_command_id":
        object.__setattr__(context.result_event_seed, "command_id", "tampered-command")
    elif tamper == "seed_correlation_id":
        object.__setattr__(
            context.result_event_seed,
            "correlation_id",
            "tampered-correlation",
        )
    else:
        object.__setattr__(
            context.result_event_seed,
            "timestamp",
            context.result_event_seed.timestamp.replace(year=2025),
        )

    outcome = builder_module._compose_participant_apply_plan(
        context=context,
        current=current,
        decision=decision,
        evidence=evidence,
        ownership=ownership,
    )

    assert outcome == BuildNonCommit(
        BuildNonCommitReason.INVALID_CONTEXT,
        "PARTICIPANT_COMPOSITION_INVALID",
    )


def test_private_composer_requires_context_owned_current_snapshot_identity():
    context = _context()
    context_current = context.session_view.current_game_snapshot
    evidence = context.setup_participant_evidence.character_assignment
    assert context_current is not None and evidence is not None
    view = next(
        view for view in context.participant_views if view.participant_id == "player-old"
    )
    decision = transition_participant(
        AssignCharacterTransitionRequest(
            ParticipantTransitionRecordState(
                view.participant_id,
                view.participant_type,
                view.membership_state,
                view.character_id,
                view.binding_version,
            ),
            "character-1",
        )
    )
    assert isinstance(decision, AssignCharacterTransitionAccepted)

    outcome = builder_module._compose_participant_apply_plan(
        context=context,
        current=replace(context_current),
        decision=decision,
        evidence=evidence,
        ownership=outcome_ownership(context),
    )

    assert outcome == BuildNonCommit(
        BuildNonCommitReason.INVALID_CONTEXT,
        "PARTICIPANT_COMPOSITION_INVALID",
    )


def test_availability_must_match_the_actual_roster_before_business_mapping():
    context = _context(availability=CharacterAvailabilityStatus.ASSIGNED_TO_TARGET)
    outcome = ParticipantControlApplyPlanBuilder().build(context)
    assert outcome == BuildNonCommit(
        BuildNonCommitReason.EVIDENCE_MISMATCH,
        "CHARACTER_AVAILABILITY_BINDING_MISMATCH",
    )


def test_multiple_roster_occupants_never_match_assignment_availability_evidence():
    context = _context(availability=CharacterAvailabilityStatus.ASSIGNED_TO_OTHER)
    participant_views = tuple(
        replace(view, character_id="character-1")
        if view.participant_id in {"player-old", "player-new"}
        else view
        for view in context.participant_views
    )
    current = context.session_view.current_game_snapshot
    assert current is not None
    records = tuple(
        ParticipantSnapshotRecord(
            participant_id=view.participant_id,
            participant_type=view.participant_type,
            membership_state=view.membership_state,
            character_id=view.character_id,
            binding_version=view.binding_version,
        )
        for view in sorted(participant_views, key=lambda view: view.participant_id)
    )
    context = replace(
        context,
        participant_views=participant_views,
        session_view=replace(
            context.session_view,
            current_game_snapshot=replace(
                current,
                participants=ParticipantSnapshotSlice(1, 1, records),
            ),
        ),
    )
    assert ParticipantControlApplyPlanBuilder().build(context) == BuildNonCommit(
        BuildNonCommitReason.EVIDENCE_MISMATCH,
        "CHARACTER_AVAILABILITY_BINDING_MISMATCH",
    )


@pytest.mark.parametrize(
    ("status", "phase", "corruption"),
    [
        (GameSessionStatus.RUNNING, GamePhase.INTRODUCTION, "failed"),
        (GameSessionStatus.ENDED, GamePhase.ENDING, "ownership"),
    ],
)
def test_lifecycle_rejections_require_verified_exact_evidence_first(status, phase, corruption):
    context = _context(status=status, phase=phase)
    if corruption == "failed":
        evidence = context.setup_participant_evidence.character_assignment
        assert evidence is not None
        context = replace(context, setup_participant_evidence=ControlSetupParticipantApplyEvidence(character_assignment=replace(evidence, validation_status=SetupParticipantEvidenceStatus.FAILED)))
    else:
        object.__setattr__(context.ownership_evidence, "active_generation", 2)
    outcome = ParticipantControlApplyPlanBuilder().build(context)
    assert isinstance(outcome, BuildNonCommit)
    assert not hasattr(outcome, "plan")


def test_snapshot_materialization_cursor_may_lag_committed_control_cursor():
    outcome = ParticipantControlApplyPlanBuilder().build(_context(snapshot_cursor=5))
    assert isinstance(outcome, BuildPlanReady)
    assert outcome.plan.expected_cursor == 6
    assert outcome.plan.candidate_snapshot.last_applied_sequence_no == 7


def test_snapshot_cursor_ahead_of_committed_cursor_fails_closed():
    context = _context()
    current = context.session_view.current_game_snapshot
    assert current is not None
    object.__setattr__(current, "last_applied_sequence_no", 7)
    outcome = ParticipantControlApplyPlanBuilder().build(context)
    assert isinstance(outcome, BuildNonCommit)
    assert not hasattr(outcome, "plan")


def test_typed_composer_mismatch_cannot_emit_a_plan():
    context = _context()
    current = context.session_view.current_game_snapshot
    evidence = context.setup_participant_evidence.character_assignment
    assert current is not None and evidence is not None
    view = next(view for view in context.participant_views if view.participant_id == "player-old")
    decision = transition_participant(
        AssignCharacterTransitionRequest(
            ParticipantTransitionRecordState(view.participant_id, view.participant_type, view.membership_state, view.character_id, view.binding_version),
            "character-1",
        )
    )
    assert isinstance(decision, AssignCharacterTransitionAccepted)
    outcome = builder_module._compose_participant_apply_plan(
        context=context,
        current=current,
        decision=replace(decision, resulting_participant=replace(decision.resulting_participant, binding_version=99)),
        evidence=evidence,
        ownership=outcome_ownership(context),
    )
    assert outcome == BuildNonCommit(
        BuildNonCommitReason.INVALID_CONTEXT,
        "PARTICIPANT_COMPOSITION_INVALID",
    )


@pytest.mark.parametrize(
    "slice_name",
    ("lifecycle", "phase", "setup", "game_rules", "hidden_state"),
)
def test_every_frozen_current_slice_is_revalidated_before_participant_composition(slice_name):
    context = _context()
    current = context.session_view.current_game_snapshot
    assert current is not None
    target = getattr(current, slice_name)
    object.__setattr__(target, "schema_version", 3)
    outcome = ParticipantControlApplyPlanBuilder().build(context)
    assert isinstance(outcome, BuildNonCommit)
    assert not hasattr(outcome, "plan")


def test_typed_composer_cannot_replace_an_unknown_participant():
    context = _context()
    current = context.session_view.current_game_snapshot
    evidence = context.setup_participant_evidence.character_assignment
    assert current is not None and evidence is not None
    view = next(view for view in context.participant_views if view.participant_id == "player-old")
    decision = transition_participant(
        AssignCharacterTransitionRequest(
            ParticipantTransitionRecordState(view.participant_id, view.participant_type, view.membership_state, view.character_id, view.binding_version),
            "character-1",
        )
    )
    assert isinstance(decision, AssignCharacterTransitionAccepted)
    outcome = builder_module._compose_participant_apply_plan(
        context=context,
        current=current,
        decision=replace(decision, resulting_participant=replace(decision.resulting_participant, participant_id="unknown-player")),
        evidence=evidence,
        ownership=outcome_ownership(context),
    )
    assert outcome == BuildNonCommit(
        BuildNonCommitReason.INVALID_CONTEXT,
        "PARTICIPANT_COMPOSITION_INVALID",
    )


def outcome_ownership(context):
    ownership = builder_module._ownership_intent(context, context.ownership_evidence.active_generation)
    assert not isinstance(ownership, BuildNonCommit)
    return ownership


def test_running_assignment_roster_mismatch_precedes_lifecycle_reject():
    outcome = ParticipantControlApplyPlanBuilder().build(
        _context(
            status=GameSessionStatus.RUNNING,
            phase=GamePhase.INTRODUCTION,
            availability=CharacterAvailabilityStatus.ASSIGNED_TO_TARGET,
        )
    )
    assert outcome == BuildNonCommit(
        BuildNonCommitReason.EVIDENCE_MISMATCH,
        "CHARACTER_AVAILABILITY_BINDING_MISMATCH",
    )


def test_ended_replacement_character_reference_mismatch_precedes_lifecycle_reject():
    context = _context(
        SessionCommandType.REPLACE_PLAYER,
        status=GameSessionStatus.ENDED,
        phase=GamePhase.ENDING,
        old_character=None,
    )
    evidence = context.setup_participant_evidence.player_replacement
    assert evidence is not None
    context = replace(
        context,
        setup_participant_evidence=ControlSetupParticipantApplyEvidence(
            player_replacement=replace(
                evidence,
                character_binding_reference="unexpected-binding",
            )
        ),
    )
    outcome = ParticipantControlApplyPlanBuilder().build(context)
    assert outcome == BuildNonCommit(
        BuildNonCommitReason.EVIDENCE_MISMATCH,
        "REPLACEMENT_CHARACTER_BINDING_MISMATCH",
    )


def test_typed_assign_composer_rejects_frozen_effect_tampering():
    context = _context()
    current = context.session_view.current_game_snapshot
    evidence = context.setup_participant_evidence.character_assignment
    assert current is not None and evidence is not None
    view = next(view for view in context.participant_views if view.participant_id == "player-old")
    decision = transition_participant(AssignCharacterTransitionRequest(
        ParticipantTransitionRecordState(view.participant_id, view.participant_type, view.membership_state, view.character_id, view.binding_version),
        "character-1",
    ))
    assert isinstance(decision, AssignCharacterTransitionAccepted)
    object.__setattr__(decision, "effect", ParticipantTransitionEffect.REPLACE_PLAYER)
    outcome = builder_module._compose_participant_apply_plan(
        context=context, current=current, decision=decision,
        evidence=evidence, ownership=outcome_ownership(context),
    )
    assert outcome == BuildNonCommit(BuildNonCommitReason.INVALID_CONTEXT, "PARTICIPANT_COMPOSITION_INVALID")


def test_private_composer_revalidates_nested_snapshot_and_ownership():
    context = _context()
    current = context.session_view.current_game_snapshot
    evidence = context.setup_participant_evidence.character_assignment
    assert current is not None and evidence is not None
    view = next(view for view in context.participant_views if view.participant_id == "player-old")
    decision = transition_participant(AssignCharacterTransitionRequest(
        ParticipantTransitionRecordState(view.participant_id, view.participant_type, view.membership_state, view.character_id, view.binding_version),
        "character-1",
    ))
    assert isinstance(decision, AssignCharacterTransitionAccepted)
    object.__setattr__(current.game_rules, "schema_version", 2)
    outcome = builder_module._compose_participant_apply_plan(
        context=context, current=current, decision=decision,
        evidence=evidence, ownership=OwnershipIntent(OwnershipIntentType.RETAIN, 1, 1),
    )
    assert outcome == BuildNonCommit(BuildNonCommitReason.INVALID_CONTEXT, "PARTICIPANT_COMPOSITION_INVALID")


def test_private_composer_rejects_created_retain_ownership_even_with_valid_snapshot():
    context = _context()
    current = context.session_view.current_game_snapshot
    evidence = context.setup_participant_evidence.character_assignment
    assert current is not None and evidence is not None
    view = next(view for view in context.participant_views if view.participant_id == "player-old")
    decision = transition_participant(AssignCharacterTransitionRequest(
        ParticipantTransitionRecordState(view.participant_id, view.participant_type, view.membership_state, view.character_id, view.binding_version),
        "character-1",
    ))
    assert isinstance(decision, AssignCharacterTransitionAccepted)
    outcome = builder_module._compose_participant_apply_plan(
        context=context, current=current, decision=decision,
        evidence=evidence, ownership=OwnershipIntent(OwnershipIntentType.RETAIN, 1, 1),
    )
    assert outcome == BuildNonCommit(BuildNonCommitReason.INVALID_CONTEXT, "PARTICIPANT_COMPOSITION_INVALID")


@pytest.mark.parametrize("corruption", ("old_membership", "new_type", "effect", "binding_reference"))
def test_typed_replace_composer_rejects_each_transition_and_reference_mismatch(corruption):
    context = _context(SessionCommandType.REPLACE_PLAYER)
    current = context.session_view.current_game_snapshot
    evidence = context.setup_participant_evidence.player_replacement
    assert current is not None and evidence is not None
    old = next(view for view in context.participant_views if view.participant_id == "player-old")
    new = next(view for view in context.participant_views if view.participant_id == "player-new")
    decision = transition_participant(ReplacePlayerTransitionRequest(
        ParticipantTransitionRecordState(old.participant_id, old.participant_type, old.membership_state, old.character_id, old.binding_version),
        ParticipantTransitionRecordState(new.participant_id, new.participant_type, new.membership_state, new.character_id, new.binding_version),
    ))
    assert isinstance(decision, ReplacePlayerTransitionAccepted)
    if corruption == "old_membership":
        decision = replace(decision, resulting_old_participant=replace(decision.resulting_old_participant, membership_state=ParticipantMembershipState.ACTIVE))
    elif corruption == "new_type":
        decision = replace(decision, resulting_new_participant=replace(decision.resulting_new_participant, participant_type=ParticipantType.DM))
    elif corruption == "effect":
        object.__setattr__(decision, "effect", ParticipantTransitionEffect.ASSIGN_CHARACTER)
    else:
        evidence = replace(evidence, character_binding_reference=None)
    outcome = builder_module._compose_participant_apply_plan(
        context=context, current=current, decision=decision,
        evidence=evidence, ownership=outcome_ownership(context),
    )
    expected = (
        BuildNonCommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "REPLACEMENT_CHARACTER_BINDING_MISMATCH",
        )
        if corruption == "binding_reference"
        else BuildNonCommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "PARTICIPANT_COMPOSITION_INVALID",
        )
    )
    assert outcome == expected


@pytest.mark.parametrize("corruption", ("active_generation", "evidence_generation", "scope", "version"))
def test_private_composer_revalidates_all_ownership_sources(corruption):
    context = _context()
    current = context.session_view.current_game_snapshot
    evidence = context.setup_participant_evidence.character_assignment
    assert current is not None and evidence is not None
    view = next(view for view in context.participant_views if view.participant_id == "player-old")
    decision = transition_participant(AssignCharacterTransitionRequest(
        ParticipantTransitionRecordState(view.participant_id, view.participant_type, view.membership_state, view.character_id, view.binding_version),
        "character-1",
    ))
    assert isinstance(decision, AssignCharacterTransitionAccepted)
    if corruption == "active_generation":
        object.__setattr__(context.ownership_evidence, "active_generation", 2)
    elif corruption == "evidence_generation":
        evidence = replace(evidence, ownership_generation=2)
    elif corruption == "scope":
        object.__setattr__(context.ownership_evidence, "group_id", "other-group")
    else:
        object.__setattr__(context.ownership_evidence, "observed_state_version", 3)
    outcome = builder_module._compose_participant_apply_plan(
        context=context, current=current, decision=decision, evidence=evidence,
        ownership=OwnershipIntent(OwnershipIntentType.UNCHANGED, None, None),
    )
    assert outcome == BuildNonCommit(BuildNonCommitReason.INVALID_CONTEXT, "PARTICIPANT_COMPOSITION_INVALID")


def test_typed_assign_composer_rejects_assigned_previous_and_nonavailable_evidence():
    context = _context()
    current = context.session_view.current_game_snapshot
    evidence = context.setup_participant_evidence.character_assignment
    assert current is not None and evidence is not None
    participant_views = tuple(
        replace(view, character_id="old-character") if view.participant_id == "player-old" else view
        for view in context.participant_views
    )
    records = tuple(
        ParticipantSnapshotRecord(view.participant_id, view.participant_type, view.membership_state, view.character_id, view.binding_version)
        for view in sorted(participant_views, key=lambda view: view.participant_id)
    )
    context = replace(
        context,
        participant_views=participant_views,
        session_view=replace(context.session_view, current_game_snapshot=replace(current, participants=ParticipantSnapshotSlice(1, 1, records))),
    )
    current = context.session_view.current_game_snapshot
    assert current is not None
    previous = ParticipantTransitionRecordState("player-old", ParticipantType.PLAYER, ParticipantMembershipState.ACTIVE, "old-character", 2)
    resulting = replace(previous, character_id="character-1", binding_version=3)
    decision = AssignCharacterTransitionAccepted(previous, resulting, ParticipantTransitionEffect.ASSIGN_CHARACTER)
    nonavailable = replace(
        evidence,
        availability_evidence=replace(
            evidence.availability_evidence,
            availability_status=CharacterAvailabilityStatus.ASSIGNED_TO_OTHER,
            occupying_participant_reference="player-new",
        ),
    )
    outcome = builder_module._compose_participant_apply_plan(
        context=context, current=current, decision=decision, evidence=nonavailable,
        ownership=outcome_ownership(context),
    )
    assert outcome == BuildNonCommit(BuildNonCommitReason.INVALID_CONTEXT, "PARTICIPANT_COMPOSITION_INVALID")


def test_typed_assign_composer_rejects_other_current_occupant_of_target_character():
    context = _context()
    current = context.session_view.current_game_snapshot
    evidence = context.setup_participant_evidence.character_assignment
    assert current is not None and evidence is not None
    participant_views = tuple(
        replace(view, character_id="character-1") if view.participant_id == "player-new" else view
        for view in context.participant_views
    )
    records = tuple(ParticipantSnapshotRecord(view.participant_id, view.participant_type, view.membership_state, view.character_id, view.binding_version) for view in sorted(participant_views, key=lambda view: view.participant_id))
    context = replace(context, participant_views=participant_views, session_view=replace(context.session_view, current_game_snapshot=replace(current, participants=ParticipantSnapshotSlice(1, 1, records))))
    current = context.session_view.current_game_snapshot
    assert current is not None
    view = next(view for view in context.participant_views if view.participant_id == "player-old")
    decision = transition_participant(AssignCharacterTransitionRequest(ParticipantTransitionRecordState(view.participant_id, view.participant_type, view.membership_state, view.character_id, view.binding_version), "character-1"))
    assert isinstance(decision, AssignCharacterTransitionAccepted)
    outcome = builder_module._compose_participant_apply_plan(context=context, current=current, decision=decision, evidence=evidence, ownership=outcome_ownership(context))
    assert outcome == BuildNonCommit(BuildNonCommitReason.INVALID_CONTEXT, "PARTICIPANT_COMPOSITION_INVALID")


@pytest.mark.parametrize("corruption", ("availability_version", "assignment_contract"))
def test_private_composer_reconstructs_assign_evidence_before_composition(corruption):
    context = _context()
    current = context.session_view.current_game_snapshot
    evidence = context.setup_participant_evidence.character_assignment
    assert current is not None and evidence is not None
    view = next(view for view in context.participant_views if view.participant_id == "player-old")
    decision = transition_participant(AssignCharacterTransitionRequest(ParticipantTransitionRecordState(view.participant_id, view.participant_type, view.membership_state, view.character_id, view.binding_version), "character-1"))
    assert isinstance(decision, AssignCharacterTransitionAccepted)
    if corruption == "availability_version":
        object.__setattr__(evidence.availability_evidence, "observed_state_version", 99)
    else:
        object.__setattr__(evidence, "contract_version", 2)
    outcome = builder_module._compose_participant_apply_plan(context=context, current=current, decision=decision, evidence=evidence, ownership=outcome_ownership(context))
    assert outcome == BuildNonCommit(BuildNonCommitReason.INVALID_CONTEXT, "PARTICIPANT_COMPOSITION_INVALID")


@pytest.mark.parametrize("corruption", ("contract", "scope"))
def test_private_composer_reconstructs_replacement_evidence_before_composition(corruption):
    context = _context(SessionCommandType.REPLACE_PLAYER)
    current = context.session_view.current_game_snapshot
    evidence = context.setup_participant_evidence.player_replacement
    assert current is not None and evidence is not None
    old = next(view for view in context.participant_views if view.participant_id == "player-old")
    new = next(view for view in context.participant_views if view.participant_id == "player-new")
    decision = transition_participant(ReplacePlayerTransitionRequest(ParticipantTransitionRecordState(old.participant_id, old.participant_type, old.membership_state, old.character_id, old.binding_version), ParticipantTransitionRecordState(new.participant_id, new.participant_type, new.membership_state, new.character_id, new.binding_version)))
    assert isinstance(decision, ReplacePlayerTransitionAccepted)
    if corruption == "contract":
        object.__setattr__(evidence, "contract_version", 2)
    else:
        object.__setattr__(evidence, "session_id", "other-session")
    outcome = builder_module._compose_participant_apply_plan(context=context, current=current, decision=decision, evidence=evidence, ownership=outcome_ownership(context))
    assert outcome == BuildNonCommit(BuildNonCommitReason.INVALID_CONTEXT, "PARTICIPANT_COMPOSITION_INVALID")
