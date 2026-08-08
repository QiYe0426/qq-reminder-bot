from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

import game_runtime.session_control as session_control
from game_runtime.event import (
    GameEventType,
    QuestActivatedPayload,
    SessionControlRejectedPayload,
    validate_control_result_event,
)
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control import (
    ActivateQuestPayload,
    BuildNonCommit,
    BuildNonCommitReason,
    BuildPlanReady,
    BuildReject,
    CanonicalControlCommandIntent,
    CompositeGameControlApplyPlanBuilder,
    ControlGameRuleApplyEvidence,
    ControlQuestActivationEvidence,
    QuestActivationDisposition,
    QuestEvidenceStatus,
    SessionCommandType,
)
from game_runtime.session_control.confirmation import fingerprint_payload
from game_runtime.session_control.event_integration import DMCommandEventPayload
from test_clue_reveal_control_plane_contract import _reveal_context


def _contract(name: str):
    value = getattr(session_control, name, None)
    assert value is not None, f"missing contract: {name}"
    return value


def _quest_context(
    *,
    status: GameSessionStatus = GameSessionStatus.RUNNING,
    phase: GamePhase = GamePhase.EXPLORATION,
    disposition: QuestActivationDisposition = QuestActivationDisposition.AVAILABLE,
):
    base = _reveal_context(status=status, phase=phase)
    snapshot = base.session_view.current_game_snapshot
    assert snapshot is not None
    active = disposition in {
        QuestActivationDisposition.ALREADY_ACTIVE,
        QuestActivationDisposition.CONFLICT,
    }
    rule_active = disposition is not QuestActivationDisposition.RULE_SET_NOT_ACTIVE
    available = disposition is QuestActivationDisposition.AVAILABLE
    payload = ActivateQuestPayload(
        "quest-1",
        snapshot.game_rules.domain_version if rule_active else 0,
        1 if active else 0,
        snapshot.hidden_state.domain_version if rule_active else 0,
    )
    fingerprint = fingerprint_payload(payload)
    event_payload = DMCommandEventPayload(
        command_id=base.envelope.command_id,
        command_type=SessionCommandType.ACTIVATE_QUEST,
        requester="dm-1",
        causation_event_id="request-1",
        observed_state_version=base.session_view.state_version,
        payload_reference=f"sha256:{fingerprint}",
        payload_fingerprint=fingerprint,
    )
    event = replace(base.envelope.event, payload=event_payload.to_mapping())
    envelope = replace(base.envelope, event=event)
    current_quest_id = None
    current_public_reference = None
    quest_version = 0
    if active:
        current_quest_id = (
            "quest-1"
            if disposition is QuestActivationDisposition.ALREADY_ACTIVE
            else "quest-other"
        )
        current_public_reference = "quest-public:current"
        quest_version = 1
        active_quest = session_control.QuestSnapshotSlice(
            1,
            1,
            current_quest_id,
            "rule-set:commit-1",
            current_public_reference,
        )
        snapshot = replace(snapshot, quest=active_quest)
    if not rule_active:
        snapshot = replace(
            snapshot,
            game_rules=session_control.GameRuleSnapshotSlice(
                2, 0, None, None
            ),
            hidden_state=session_control.HiddenGameStateSlice(1, 0, None),
        )
    evidence = ControlQuestActivationEvidence(
        evidence_schema_version=1,
        game_id=snapshot.game_id,
        session_id=snapshot.session_id,
        observed_state_version=snapshot.state_version,
        quest_id="quest-1",
        active_rule_set_reference=(
            snapshot.game_rules.committed_rule_set_reference
        ),
        current_active_quest_id=current_quest_id,
        current_public_state_reference=current_public_reference,
        resulting_public_state_reference=(
            "quest-public:commit-1" if available else None
        ),
        current_hidden_state_reference=(
            snapshot.hidden_state.committed_state_reference
        ),
        resulting_hidden_state_reference=(
            "hidden-state:quest-commit-1" if available else None
        ),
        game_rule_version=snapshot.game_rules.domain_version,
        quest_version=quest_version,
        hidden_state_version=snapshot.hidden_state.domain_version,
        provenance_reference=("quest-provenance:1" if available else None),
        disposition=disposition,
        validation_status=QuestEvidenceStatus.VERIFIED,
    )
    intent = CanonicalControlCommandIntent(
        intent_schema_version=1,
        command_type=SessionCommandType.ACTIVATE_QUEST,
        payload=payload,
        payload_fingerprint=fingerprint,
    )
    session_view = replace(
        base.session_view,
        current_game_snapshot=snapshot,
        game_rule_evidence=ControlGameRuleApplyEvidence(),
        quest_activation_evidence=evidence,
    )
    return replace(
        base,
        envelope=envelope,
        command_intent=intent,
        session_view=session_view,
    )


def test_quest_activation_mutation_is_frozen_and_advances_once() -> None:
    mutation_type = _contract("QuestActivationMutation")
    mutation = mutation_type(
        mutation_type=_contract("QuestMutationType").ACTIVATE_QUEST,
        quest_id="quest-1",
        source_rule_set_reference="rule-set:commit-1",
        public_state_reference="quest-public:commit-1",
        current_hidden_state_reference="hidden-state:commit-1",
        resulting_hidden_state_reference="hidden-state:quest-commit-1",
        expected_game_rule_version=1,
        expected_quest_version=0,
        resulting_quest_version=1,
        expected_hidden_state_version=1,
        resulting_hidden_state_version=2,
        provenance_reference="quest-provenance:1",
    )

    assert not hasattr(mutation, "__dict__")
    with pytest.raises(FrozenInstanceError):
        mutation.quest_id = "quest-other"
    with pytest.raises(ValueError):
        replace(mutation, resulting_quest_version=2)


def test_available_quest_composes_complete_apply_plan() -> None:
    context = _quest_context()
    before = context.session_view.current_game_snapshot
    builder = _contract("QuestControlApplyPlanBuilder")()

    outcome = builder.build(context)

    assert isinstance(outcome, BuildPlanReady)
    candidate = outcome.plan.candidate_snapshot
    assert candidate.state_version == before.state_version + 1
    assert candidate.last_applied_sequence_no == context.envelope.event_sequence_no
    assert candidate.lifecycle is before.lifecycle
    assert candidate.phase is before.phase
    assert candidate.setup is before.setup
    assert candidate.participants is before.participants
    assert candidate.game_rules is before.game_rules
    assert candidate.quest.active_quest_id == "quest-1"
    assert candidate.quest.domain_version == 1
    assert candidate.hidden_state.domain_version == (
        before.hidden_state.domain_version + 1
    )
    assert len(outcome.plan.quest_mutations) == 1
    assert outcome.plan.quest_activation_evidence == (
        context.session_view.quest_activation_evidence
    )
    assert tuple(event.event_type for event in outcome.plan.result_events) == (
        GameEventType.QUEST_ACTIVATED,
    )
    payload = validate_control_result_event(outcome.plan.result_events[0])
    assert isinstance(payload, QuestActivatedPayload)
    assert payload.quest_id == "quest-1"


@pytest.mark.parametrize(
    ("status", "phase", "disposition", "reason"),
    [
        (
            GameSessionStatus.CREATED,
            GamePhase.LOBBY,
            QuestActivationDisposition.AVAILABLE,
            "INVALID_LIFECYCLE",
        ),
        (
            GameSessionStatus.RUNNING,
            GamePhase.DISCUSSION,
            QuestActivationDisposition.AVAILABLE,
            "INVALID_PHASE",
        ),
        (
            GameSessionStatus.RUNNING,
            GamePhase.EXPLORATION,
            QuestActivationDisposition.ALREADY_ACTIVE,
            "QUEST_ALREADY_ACTIVE",
        ),
        (
            GameSessionStatus.RUNNING,
            GamePhase.EXPLORATION,
            QuestActivationDisposition.CONFLICT,
            "QUEST_CONFLICT",
        ),
        (
            GameSessionStatus.RUNNING,
            GamePhase.EXPLORATION,
            QuestActivationDisposition.NOT_FOUND,
            "QUEST_NOT_FOUND",
        ),
        (
            GameSessionStatus.RUNNING,
            GamePhase.EXPLORATION,
            QuestActivationDisposition.NOT_ACTIVATABLE,
            "QUEST_NOT_ACTIVATABLE",
        ),
        (
            GameSessionStatus.RUNNING,
            GamePhase.EXPLORATION,
            QuestActivationDisposition.RULE_SET_NOT_ACTIVE,
            "RULE_SET_NOT_ACTIVE",
        ),
    ],
)
def test_business_rejections_have_no_candidate(
    status: GameSessionStatus,
    phase: GamePhase,
    disposition: QuestActivationDisposition,
    reason: str,
) -> None:
    outcome = _contract("QuestControlApplyPlanBuilder")().build(
        _quest_context(status=status, phase=phase, disposition=disposition)
    )

    assert isinstance(outcome, BuildReject)
    payload = validate_control_result_event(outcome.plan.rejection_event)
    assert isinstance(payload, SessionControlRejectedPayload)
    assert payload.reason_code == reason


def test_unknown_or_mismatched_evidence_fails_closed() -> None:
    context = _quest_context()
    evidence = context.session_view.quest_activation_evidence
    assert evidence is not None
    object.__setattr__(
        evidence,
        "disposition",
        QuestActivationDisposition.UNKNOWN,
    )

    outcome = _contract("QuestControlApplyPlanBuilder")().build(context)

    assert outcome == BuildNonCommit(
        BuildNonCommitReason.EVIDENCE_MISMATCH,
        "ACTIVATE_QUEST_EVIDENCE_INVALID",
    )


def test_builder_and_composite_dispatcher_are_deterministic_and_equivalent() -> None:
    context = _quest_context(phase=GamePhase.INTRODUCTION)
    builder = _contract("QuestControlApplyPlanBuilder")()

    first = builder.build(context)
    second = builder.build(context)
    dispatched = CompositeGameControlApplyPlanBuilder().build(context)

    assert first == second == dispatched
    assert isinstance(first, BuildPlanReady)
