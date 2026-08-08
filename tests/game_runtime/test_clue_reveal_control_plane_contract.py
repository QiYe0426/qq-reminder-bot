from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timezone

import pytest

import game_runtime.session_control as session_control
import game_runtime.session_control.apply_contract as apply_contract
from game_runtime.session_control import (
    BuildPlanReady,
    BuildNonCommit,
    BuildNonCommitReason,
    BuildReject,
    CandidateGameSnapshot,
    CanonicalControlCommandIntent,
    ClueRevealDisposition,
    ControlApplyBuildContext,
    ControlClueRevealEvidence,
    ControlGameRuleApplyEvidence,
    ControlSessionBuildView,
    GameRuleControlApplyPlanBuilder,
    GameRuleEvidenceStatus,
    RevealCluePayload,
    SessionCommandType,
)
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.event import (
    ClueRevealedPayload,
    EventVisibility,
    GameEvent,
    GameEventSource,
    GameEventType,
    validate_control_result_event,
)
from game_runtime.session_control.confirmation import fingerprint_payload
from game_runtime.session_control.event_integration import DMCommandEventPayload
from test_game_rule_control_plane_contract import _context as _activation_context


def _contract(name: str):
    value = getattr(session_control, name, None)
    assert value is not None, f"missing {name}"
    return value


def _available_evidence(**overrides: object):
    evidence_type = _contract("ControlClueRevealEvidence")
    disposition_type = _contract("ClueRevealDisposition")
    values: dict[str, object] = {
        "evidence_schema_version": 1,
        "game_id": "game-1",
        "session_id": "session-1",
        "observed_state_version": 8,
        "clue_id": "clue-1",
        "active_rule_set_reference": "rule-set:commit-1",
        "current_disclosure_state_reference": "disclosure-state:commit-1",
        "resulting_disclosure_state_reference": "disclosure-state:commit-2",
        "current_hidden_state_reference": "hidden-state:commit-1",
        "resulting_hidden_state_reference": "hidden-state:commit-2",
        "game_rule_version": 1,
        "hidden_state_version": 1,
        "public_disclosure_reference": "public-disclosure:clue-1",
        "provenance_reference": "provenance:clue-1",
        "disposition": disposition_type.AVAILABLE,
        "validation_status": GameRuleEvidenceStatus.VERIFIED,
    }
    values.update(overrides)
    return evidence_type(**values)


def test_clue_reveal_evidence_is_frozen_slotted_and_exactly_bound() -> None:
    evidence_type = _contract("ControlClueRevealEvidence")
    evidence = _available_evidence()
    bundle = ControlGameRuleApplyEvidence(clue_reveal=evidence)
    payload = RevealCluePayload("clue-1", 1, 1)

    assert tuple(field.name for field in fields(evidence_type)) == (
        "evidence_schema_version",
        "game_id",
        "session_id",
        "observed_state_version",
        "clue_id",
        "active_rule_set_reference",
        "current_disclosure_state_reference",
        "resulting_disclosure_state_reference",
        "current_hidden_state_reference",
        "resulting_hidden_state_reference",
        "game_rule_version",
        "hidden_state_version",
        "public_disclosure_reference",
        "provenance_reference",
        "disposition",
        "validation_status",
    )
    assert not hasattr(evidence, "__dict__")
    with pytest.raises(FrozenInstanceError):
        evidence.clue_id = "clue-other"
    bundle.validate_for_command(
        SessionCommandType.REVEAL_CLUE,
        game_id="game-1",
        session_id="session-1",
        observed_state_version=8,
    )
    bundle.validate_payload_binding(SessionCommandType.REVEAL_CLUE, payload)


def test_game_rule_evidence_bundle_enforces_command_exact_set() -> None:
    evidence = _available_evidence()
    bundle = ControlGameRuleApplyEvidence(clue_reveal=evidence)

    with pytest.raises(ValueError, match="empty Game Rule evidence"):
        bundle.validate_for_command(
            SessionCommandType.PAUSE_GAME,
            game_id="game-1",
            session_id="session-1",
            observed_state_version=8,
        )
    with pytest.raises(ValueError, match="requires rule_set_activation"):
        bundle.validate_for_command(
            SessionCommandType.ACTIVATE_RULE_SET,
            game_id="game-1",
            session_id="session-1",
            observed_state_version=8,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"resulting_disclosure_state_reference": None},
        {"resulting_hidden_state_reference": "hidden-state:commit-1"},
        {"public_disclosure_reference": None},
        {"provenance_reference": None},
        {"validation_status": GameRuleEvidenceStatus.UNKNOWN},
    ],
)
def test_available_evidence_rejects_partial_or_unverified_values(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _available_evidence(**overrides)


def test_unknown_disposition_is_representable_but_fails_command_validation() -> None:
    disposition_type = _contract("ClueRevealDisposition")
    evidence = _available_evidence(
        resulting_disclosure_state_reference=None,
        resulting_hidden_state_reference=None,
        public_disclosure_reference=None,
        provenance_reference=None,
        disposition=disposition_type.UNKNOWN,
    )
    bundle = ControlGameRuleApplyEvidence(clue_reveal=evidence)

    with pytest.raises(ValueError, match="UNKNOWN"):
        bundle.validate_for_command(
            SessionCommandType.REVEAL_CLUE,
            game_id="game-1",
            session_id="session-1",
            observed_state_version=8,
        )


def test_reveal_payload_binding_rejects_identity_or_version_mismatch() -> None:
    evidence = _available_evidence()
    bundle = ControlGameRuleApplyEvidence(clue_reveal=evidence)

    for payload in (
        RevealCluePayload("clue-other", 1, 1),
        RevealCluePayload("clue-1", 2, 1),
        RevealCluePayload("clue-1", 1, 2),
    ):
        with pytest.raises(ValueError, match="does not match"):
            bundle.validate_payload_binding(SessionCommandType.REVEAL_CLUE, payload)


def test_clue_reveal_mutation_is_closed_frozen_and_advances_both_domains_once() -> None:
    mutation_type = _contract("ClueRevealMutation")
    mutation = mutation_type(
        mutation_type=apply_contract.GameRuleMutationType.REVEAL_CLUE,
        clue_id="clue-1",
        active_rule_set_reference="rule-set:commit-1",
        public_disclosure_reference="public-disclosure:clue-1",
        current_disclosure_state_reference="disclosure-state:commit-1",
        resulting_disclosure_state_reference="disclosure-state:commit-2",
        current_hidden_state_reference="hidden-state:commit-1",
        resulting_hidden_state_reference="hidden-state:commit-2",
        expected_game_rule_version=1,
        resulting_game_rule_version=2,
        expected_hidden_state_version=1,
        resulting_hidden_state_version=2,
        provenance_reference="provenance:clue-1",
    )

    assert tuple(field.name for field in fields(mutation_type)) == (
        "mutation_type",
        "clue_id",
        "active_rule_set_reference",
        "public_disclosure_reference",
        "current_disclosure_state_reference",
        "resulting_disclosure_state_reference",
        "current_hidden_state_reference",
        "resulting_hidden_state_reference",
        "expected_game_rule_version",
        "resulting_game_rule_version",
        "expected_hidden_state_version",
        "resulting_hidden_state_version",
        "provenance_reference",
    )
    assert not hasattr(mutation, "__dict__")
    with pytest.raises(FrozenInstanceError):
        mutation.clue_id = "clue-other"
    with pytest.raises(ValueError, match="advance once"):
        replace(mutation, resulting_game_rule_version=3)
    with pytest.raises(ValueError, match="advance committed references"):
        replace(
            mutation,
            resulting_hidden_state_reference=mutation.current_hidden_state_reference,
        )


def test_activation_mutation_rejects_reveal_tag() -> None:
    with pytest.raises(ValueError, match="ACTIVATE_RULE_SET"):
        apply_contract.GameRuleMutation(
            mutation_type=apply_contract.GameRuleMutationType.REVEAL_CLUE,
            manifest_reference="manifest-1",
            committed_rule_set_reference="rule-set:commit-1",
            committed_disclosure_state_reference="disclosure-state:commit-1",
            opaque_hidden_state_reference="hidden-state:commit-1",
            expected_game_rule_version=0,
            resulting_game_rule_version=1,
            expected_hidden_state_version=0,
            resulting_hidden_state_version=1,
            provenance_reference="provenance-1",
        )


def _reveal_context(
    *,
    status: GameSessionStatus = GameSessionStatus.RUNNING,
    phase: GamePhase = GamePhase.EXPLORATION,
    disposition_name: str = "AVAILABLE",
) -> ControlApplyBuildContext:
    disposition = getattr(ClueRevealDisposition, disposition_name)
    available = disposition is ClueRevealDisposition.AVAILABLE
    active = disposition is not ClueRevealDisposition.RULE_SET_NOT_ACTIVE
    base = _activation_context(
        status=status,
        phase=phase,
        current_rule_reference="rule-set:commit-1",
        current_hidden_reference="hidden-state:commit-1",
    )
    payload = RevealCluePayload(
        "clue-1",
        1 if active else 0,
        1 if active else 0,
    )
    fingerprint = fingerprint_payload(payload)
    event = GameEvent(
        event_id="event-7",
        game_id="game-1",
        session_id="session-1",
        event_type=GameEventType.DM_COMMAND,
        actor="dm-1",
        source=GameEventSource.CONTROL,
        correlation_id="correlation-1",
        timestamp=datetime(2026, 8, 8, tzinfo=timezone.utc),
        payload=DMCommandEventPayload(
            command_id="command-1",
            command_type=SessionCommandType.REVEAL_CLUE,
            requester="dm-1",
            causation_event_id="request-1",
            observed_state_version=4,
            payload_reference=f"sha256:{fingerprint}",
            payload_fingerprint=fingerprint,
        ).to_mapping(),
        visibility=EventVisibility.DM_CONTROL,
        observed_state_version=4,
        causation_event_id="request-1",
    )
    evidence = ControlClueRevealEvidence(
        evidence_schema_version=1,
        game_id="game-1",
        session_id="session-1",
        observed_state_version=4,
        clue_id="clue-1",
        active_rule_set_reference=("rule-set:commit-1" if active else None),
        current_disclosure_state_reference=(
            "disclosure-state:current" if active else None
        ),
        resulting_disclosure_state_reference=(
            "disclosure-state:commit-2" if available else None
        ),
        current_hidden_state_reference=("hidden-state:commit-1" if active else None),
        resulting_hidden_state_reference=("hidden-state:commit-2" if available else None),
        game_rule_version=(1 if active else 0),
        hidden_state_version=(1 if active else 0),
        public_disclosure_reference=(
            "public-disclosure:clue-1" if available else None
        ),
        provenance_reference=("provenance:clue-1" if available else None),
        disposition=disposition,
        validation_status=GameRuleEvidenceStatus.VERIFIED,
    )
    snapshot = base.session_view.current_game_snapshot
    assert snapshot is not None
    if not active:
        snapshot = replace(
            snapshot,
            game_rules=replace(
                snapshot.game_rules,
                domain_version=0,
                committed_rule_set_reference=None,
                committed_disclosure_state_reference=None,
            ),
            hidden_state=replace(
                snapshot.hidden_state,
                domain_version=0,
                committed_state_reference=None,
            ),
        )
    session_view = replace(
        base.session_view,
        current_game_snapshot=snapshot,
        game_rule_evidence=ControlGameRuleApplyEvidence(clue_reveal=evidence),
    )
    return replace(
        base,
        envelope=replace(base.envelope, event=event),
        command_intent=CanonicalControlCommandIntent(
            intent_schema_version=1,
            command_type=SessionCommandType.REVEAL_CLUE,
            payload=payload,
            payload_fingerprint=fingerprint,
        ),
        session_view=session_view,
    )


def test_reveal_builder_composes_complete_candidate_mutation_and_public_event() -> None:
    context = _reveal_context()
    current = context.session_view.current_game_snapshot
    assert current is not None

    outcome = GameRuleControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildPlanReady)
    plan = outcome.plan
    candidate = plan.candidate_snapshot
    assert isinstance(candidate, CandidateGameSnapshot)
    assert candidate.state_version == current.state_version + 1
    assert candidate.last_applied_sequence_no == context.envelope.event_sequence_no
    assert candidate.lifecycle is current.lifecycle
    assert candidate.phase is current.phase
    assert candidate.setup is current.setup
    assert candidate.participants is current.participants
    assert candidate.game_rules.committed_rule_set_reference == "rule-set:commit-1"
    assert candidate.game_rules.committed_disclosure_state_reference == "disclosure-state:commit-2"
    assert candidate.game_rules.domain_version == 2
    assert candidate.hidden_state.committed_state_reference == "hidden-state:commit-2"
    assert candidate.hidden_state.domain_version == 2
    assert len(plan.game_rule_mutations) == 1
    mutation = plan.game_rule_mutations[0]
    assert isinstance(mutation, _contract("ClueRevealMutation"))
    assert mutation.clue_id == "clue-1"
    assert plan.ownership_intent.intent_type.value == "UNCHANGED"
    assert len(plan.result_events) == 1
    result_event = plan.result_events[0]
    assert result_event.event_type is GameEventType.CLUE_REVEALED
    assert result_event.visibility is EventVisibility.PUBLIC
    payload = validate_control_result_event(result_event)
    assert isinstance(payload, ClueRevealedPayload)
    assert payload.clue_id == mutation.clue_id
    assert payload.public_disclosure_reference == mutation.public_disclosure_reference
    assert payload.game_rule_domain_version == candidate.game_rules.domain_version


@pytest.mark.parametrize(
    ("status", "phase", "disposition_name", "reason"),
    [
        (
            GameSessionStatus.CREATED,
            GamePhase.LOBBY,
            "AVAILABLE",
            "INVALID_LIFECYCLE",
        ),
        (
            GameSessionStatus.RUNNING,
            GamePhase.DISCUSSION,
            "AVAILABLE",
            "INVALID_PHASE",
        ),
        (
            GameSessionStatus.RUNNING,
            GamePhase.EXPLORATION,
            "RULE_SET_NOT_ACTIVE",
            "RULE_SET_NOT_ACTIVE",
        ),
        (
            GameSessionStatus.RUNNING,
            GamePhase.EXPLORATION,
            "ALREADY_REVEALED",
            "CLUE_ALREADY_REVEALED",
        ),
        (
            GameSessionStatus.RUNNING,
            GamePhase.EXPLORATION,
            "NOT_FOUND",
            "CLUE_NOT_FOUND",
        ),
        (
            GameSessionStatus.RUNNING,
            GamePhase.EXPLORATION,
            "NOT_REVEALABLE",
            "CLUE_NOT_REVEALABLE",
        ),
    ],
)
def test_reveal_business_conditions_commit_reject_without_candidate(
    status: GameSessionStatus,
    phase: GamePhase,
    disposition_name: str,
    reason: str,
) -> None:
    context = _reveal_context(
        status=status,
        phase=phase,
        disposition_name=disposition_name,
    )

    outcome = GameRuleControlApplyPlanBuilder().build(context)

    assert isinstance(outcome, BuildReject)
    assert outcome.plan.command_type is SessionCommandType.REVEAL_CLUE
    assert outcome.plan.expected_state_version == context.session_view.state_version
    payload = validate_control_result_event(outcome.plan.rejection_event)
    assert payload.reason_code == reason
    assert payload.result_state_version == context.session_view.state_version


def test_unknown_or_reference_mismatch_fails_closed_before_business_rejection() -> None:
    unknown = GameRuleControlApplyPlanBuilder().build(
        _reveal_context(
            status=GameSessionStatus.CREATED,
            phase=GamePhase.LOBBY,
            disposition_name="UNKNOWN",
        )
    )
    context = _reveal_context()
    evidence = context.session_view.game_rule_evidence.clue_reveal
    assert evidence is not None
    mismatched_session = replace(
        context.session_view,
        game_rule_evidence=ControlGameRuleApplyEvidence(
            clue_reveal=replace(
                evidence,
                current_disclosure_state_reference="disclosure-state:other",
            )
        ),
    )
    mismatch = GameRuleControlApplyPlanBuilder().build(
        replace(context, session_view=mismatched_session)
    )

    assert unknown == BuildNonCommit(
        reason=BuildNonCommitReason.EVIDENCE_MISMATCH,
        detail_code="REVEAL_CLUE_EVIDENCE_INVALID",
    )
    assert mismatch == BuildNonCommit(
        reason=BuildNonCommitReason.EVIDENCE_MISMATCH,
        detail_code="REVEAL_CLUE_EVIDENCE_MISMATCH",
    )


def test_reveal_builder_is_deterministic() -> None:
    context = _reveal_context()
    builder = GameRuleControlApplyPlanBuilder()

    assert builder.build(context) == builder.build(context)
