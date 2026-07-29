from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timezone

import pytest

from game_runtime.session import GamePhase
from game_runtime.session_control import (
    ControlEndRetentionEvidence,
    ControlLifecycleApplyEvidence,
    ControlLifecycleEvidenceError,
    ControlPhaseVisibilityIntent,
    ControlResumeValidationEvidence,
    ControlStartReadinessEvidence,
    HunterInstanceReadinessStatus,
    PhaseVisibilityDecision,
    ResumeValidationStatus,
    SessionCommandType,
    StartReadinessStatus,
)


NOW = datetime(2026, 7, 17, 8, 30, tzinfo=timezone.utc)
SCOPE = {
    "game_id": "game-1",
    "session_id": "session-1",
    "observed_state_version": 4,
}


def _start() -> ControlStartReadinessEvidence:
    return ControlStartReadinessEvidence(
        contract_version=1,
        **SCOPE,
        readiness_status=StartReadinessStatus.READY,
        readiness_evidence_reference="readiness-1",
        setup_manifest_reference="manifest-1",
        setup_manifest_version=2,
        participant_roster_reference="roster-1",
        participant_roster_version=3,
        character_assignment_set_reference="assignments-1",
        character_assignment_version=5,
        hunter_instance_reference="hunter-1",
        hunter_instance_version=7,
        hunter_instance_status=HunterInstanceReadinessStatus.READY,
        policy_reference="policy-1",
        policy_version=11,
        template_reference="template-1",
        template_version=13,
        knowledge_partition_reference="knowledge-partitions-1",
        knowledge_partition_version=17,
    )


def _resume() -> ControlResumeValidationEvidence:
    return ControlResumeValidationEvidence(
        contract_version=1,
        **SCOPE,
        validation_status=ResumeValidationStatus.READY,
        recovery_validation_reference="recovery-validation-1",
        validated_snapshot_reference="snapshot-4",
        validated_state_version=4,
        validated_cursor=6,
        previous_pause_event_reference="event-pause-6",
        previous_pause_state_version=4,
        previous_pause_sequence_no=6,
        ownership_evidence_reference="ownership-3",
        ownership_generation=3,
    )


def _end() -> ControlEndRetentionEvidence:
    return ControlEndRetentionEvidence(
        contract_version=1,
        **SCOPE,
        retention_policy_reference="retention-policy-1",
        retention_policy_version=2,
        retention_reference="retention-1",
        retention_timestamp=NOW,
        public_result_reference="public-result-1",
    )


def _phase() -> ControlPhaseVisibilityIntent:
    return ControlPhaseVisibilityIntent(
        contract_version=1,
        game_id="game-1",
        session_id="session-1",
        expected_state_version=4,
        previous_phase=GamePhase.EXPLORATION,
        target_phase=GamePhase.DISCUSSION,
        visibility_policy_reference="visibility-policy-1",
        visibility_policy_version=3,
        decision=PhaseVisibilityDecision.CHANGE_SET,
        change_set_reference="visibility-change-1",
        intent_reference="visibility-intent-1",
    )


def test_lifecycle_evidence_values_are_frozen_and_slotted() -> None:
    values = (_start(), _resume(), _end(), _phase())

    for value in values:
        assert not hasattr(value, "__dict__")
        with pytest.raises(FrozenInstanceError):
            value.game_id = "other-game"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("command_type", "evidence"),
    [
        (
            SessionCommandType.START_GAME,
            ControlLifecycleApplyEvidence(
                start_readiness=_start(), phase_visibility_intent=_phase()
            ),
        ),
        (SessionCommandType.PAUSE_GAME, ControlLifecycleApplyEvidence()),
        (
            SessionCommandType.RESUME_GAME,
            ControlLifecycleApplyEvidence(resume_validation=_resume()),
        ),
        (
            SessionCommandType.END_GAME,
            ControlLifecycleApplyEvidence(end_retention=_end()),
        ),
        (
            SessionCommandType.CHANGE_PHASE,
            ControlLifecycleApplyEvidence(phase_visibility_intent=_phase()),
        ),
    ],
)
def test_required_evidence_mapping_accepts_exact_command_contract(
    command_type: SessionCommandType,
    evidence: ControlLifecycleApplyEvidence,
) -> None:
    evidence.validate_for_command(command_type, **SCOPE)


@pytest.mark.parametrize(
    ("command_type", "evidence"),
    [
        (SessionCommandType.START_GAME, ControlLifecycleApplyEvidence()),
        (SessionCommandType.RESUME_GAME, ControlLifecycleApplyEvidence()),
        (SessionCommandType.END_GAME, ControlLifecycleApplyEvidence()),
        (SessionCommandType.CHANGE_PHASE, ControlLifecycleApplyEvidence()),
        (
            SessionCommandType.PAUSE_GAME,
            ControlLifecycleApplyEvidence(resume_validation=_resume()),
        ),
    ],
)
def test_missing_or_extraneous_command_evidence_is_rejected(
    command_type: SessionCommandType,
    evidence: ControlLifecycleApplyEvidence,
) -> None:
    with pytest.raises(ControlLifecycleEvidenceError):
        evidence.validate_for_command(command_type, **SCOPE)


def test_evidence_scope_and_version_are_bound_to_actor_turn() -> None:
    evidence = ControlLifecycleApplyEvidence(resume_validation=_resume())

    with pytest.raises(ControlLifecycleEvidenceError):
        evidence.validate_for_command(
            SessionCommandType.RESUME_GAME,
            game_id="other-game",
            session_id="session-1",
            observed_state_version=4,
        )
    with pytest.raises(ControlLifecycleEvidenceError):
        evidence.validate_for_command(
            SessionCommandType.RESUME_GAME,
            game_id="game-1",
            session_id="session-1",
            observed_state_version=5,
        )


def test_resume_validation_binds_snapshot_pause_cursor_and_ownership() -> None:
    with pytest.raises(ControlLifecycleEvidenceError):
        replace(_resume(), validated_state_version=3)
    with pytest.raises(ControlLifecycleEvidenceError):
        replace(_resume(), previous_pause_state_version=3)
    with pytest.raises(ControlLifecycleEvidenceError):
        replace(_resume(), previous_pause_sequence_no=5)


def test_retention_and_public_result_references_are_separate() -> None:
    evidence = _end()

    assert evidence.retention_reference == "retention-1"
    assert evidence.public_result_reference == "public-result-1"
    with pytest.raises(ControlLifecycleEvidenceError):
        replace(evidence, public_result_reference=evidence.retention_reference)


def test_phase_visibility_change_set_binding_is_closed() -> None:
    with pytest.raises(ControlLifecycleEvidenceError):
        replace(_phase(), change_set_reference=None)
    with pytest.raises(ControlLifecycleEvidenceError):
        replace(
            _phase(),
            decision=PhaseVisibilityDecision.NO_CHANGE,
            change_set_reference="visibility-change-1",
        )
    with pytest.raises(ControlLifecycleEvidenceError):
        replace(_phase(), target_phase=GamePhase.EXPLORATION)


def test_lifecycle_evidence_has_no_mutable_or_runtime_dependencies() -> None:
    value_types = (
        ControlStartReadinessEvidence,
        ControlResumeValidationEvidence,
        ControlEndRetentionEvidence,
        ControlPhaseVisibilityIntent,
        ControlLifecycleApplyEvidence,
    )
    forbidden = (
        "actor",
        "mailbox",
        "repository",
        "port",
        "callback",
        "task",
        "loader",
        "snapshot_container",
        "state_writer",
    )

    for value_type in value_types:
        for field in fields(value_type):
            field_contract = f"{field.name} {field.type}".lower()
            assert not any(term in field_contract for term in forbidden)
