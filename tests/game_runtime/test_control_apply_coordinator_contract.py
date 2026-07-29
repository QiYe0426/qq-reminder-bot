from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest

from game_runtime.event import (
    EventVisibility,
    GameEvent,
    GameEventSource,
    GameEventType,
    SessionControlRejectedPayload,
)
from game_runtime.participant import ParticipantMembershipState, ParticipantType
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control.apply_contract import (
    ControlApplyConflictReason,
    ControlApplyReceipt,
    ControlOperationClaim,
    ControlRejectPlan,
)
from game_runtime.session_control.apply_coordinator import (
    ActorOwnedApplyCoordinator,
    CoordinatorApplyConflict,
    CoordinatorApplyUnknown,
    CoordinatorApplyUnknownReason,
    CoordinatorNonCommit,
    CoordinatorClaimConflict,
    CoordinatorClaimUnknown,
    CoordinatorClaimUnknownReason,
)
from game_runtime.session_control.apply_plan_builder import (
    BuildNonCommit,
    BuildNonCommitReason,
    BuildReject,
)
from game_runtime.session_control.build_context import (
    CanonicalControlCommandIntent,
    ControlOwnershipBuildEvidence,
    ControlParticipantBuildView,
    ControlSessionBuildView,
    ControlSetupBuildView,
)
from game_runtime.session_control.commands import PauseGamePayload, SessionCommandType
from game_runtime.session_control.confirmation import fingerprint_payload
from game_runtime.session_control.coordinator_evidence import (
    ActorValidatedControlTurnEvidence,
    ControlClaimAttemptEvidence,
    CoordinatorCommitReturned,
    CoordinatorEvidenceError,
    derive_control_claim_id,
)
from game_runtime.session_control.delivery import ControlEventDeliveryEnvelope
from game_runtime.session_control.event_integration import DMCommandEventPayload
from game_runtime.session_control.operation import ControlOperationStatus
from game_runtime.session_control.lifecycle_evidence import ControlLifecycleApplyEvidence
from game_runtime.session_control.setup_participant_evidence import (
    ControlSetupParticipantApplyEvidence,
)


NOW = datetime(2026, 7, 16, 9, 45, tzinfo=timezone.utc)


def make_envelope() -> ControlEventDeliveryEnvelope:
    payload = PauseGamePayload(reason_code="DM_REQUEST")
    payload_fingerprint = fingerprint_payload(payload)
    event_payload = DMCommandEventPayload(
        command_id="command-1",
        command_type=SessionCommandType.PAUSE_GAME,
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
    return ControlEventDeliveryEnvelope(
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


def make_validated_evidence() -> ActorValidatedControlTurnEvidence:
    envelope = make_envelope()
    payload = PauseGamePayload(reason_code="DM_REQUEST")
    payload_fingerprint = fingerprint_payload(payload)
    return ActorValidatedControlTurnEvidence(
        envelope=envelope,
        command_intent=CanonicalControlCommandIntent(
            intent_schema_version=1,
            command_type=SessionCommandType.PAUSE_GAME,
            payload=payload,
            payload_fingerprint=payload_fingerprint,
        ),
        session_view=ControlSessionBuildView(
            game_id="game-1",
            session_id="session-1",
            group_id="group-1",
            dm_participant_id="participant-dm",
            status=GameSessionStatus.RUNNING,
            current_phase=GamePhase.EXPLORATION,
            state_version=4,
            last_applied_sequence_no=6,
        ),
        participant_views=[
            ControlParticipantBuildView(
                game_id="game-1",
                session_id="session-1",
                participant_id="participant-dm",
                participant_type=ParticipantType.DM,
                membership_state=ParticipantMembershipState.ACTIVE,
                character_id=None,
                binding_version=2,
            )
        ],
        setup_view=ControlSetupBuildView(
            game_id="game-1",
            session_id="session-1",
            script_id="script-1",
            public_name="Public Script",
            manifest_reference="manifest-1",
            setup_version=1,
        ),
        ownership_evidence=ControlOwnershipBuildEvidence(
            game_id="game-1",
            session_id="session-1",
            group_id="group-1",
            active_generation=3,
            last_allocated_generation=3,
            observed_state_version=4,
        ),
        governance_schema_version=1,
        authorization_reference="authorization-1",
        confirmation_reference="confirmation-1",
        lifecycle_evidence=ControlLifecycleApplyEvidence(),
        setup_participant_evidence=ControlSetupParticipantApplyEvidence(),
        claim_attempt=ControlClaimAttemptEvidence.from_envelope(
            envelope,
            claimed_at=NOW,
        ),
    )


def make_claim() -> ControlOperationClaim:
    attempt = make_validated_evidence().claim_attempt
    return ControlOperationClaim(
        game_id=attempt.game_id,
        session_id=attempt.session_id,
        command_id=attempt.command_id,
        operation_id=attempt.operation_id,
        input_event_id=attempt.input_event_id,
        claim_id=attempt.claim_id,
        claimed_at=attempt.claimed_at,
    )


def make_build_reject() -> BuildReject:
    rejection_payload = SessionControlRejectedPayload(
        command_id="command-1",
        operation_id="operation-1",
        input_event_id="event-7",
        result_code="STALE_VERSION",
        result_state_version=4,
        reason_code="STALE_VERSION",
        state_version=4,
    )
    rejection_event = GameEvent(
        event_id="event-rejected-1",
        game_id="game-1",
        session_id="session-1",
        event_type=GameEventType.SESSION_CONTROL_REJECTED,
        actor="session-actor",
        source=GameEventSource.CONTROL,
        correlation_id="correlation-1",
        timestamp=NOW,
        payload=rejection_payload.to_mapping(),
        visibility=EventVisibility.DM_CONTROL,
        observed_state_version=4,
        causation_event_id="event-7",
    )
    return BuildReject(
        plan=ControlRejectPlan(
            game_id="game-1",
            session_id="session-1",
            group_id="group-1",
            command_id="command-1",
            command_type=SessionCommandType.PAUSE_GAME,
            operation_id="operation-1",
            operation_claim_id=make_claim().claim_id,
            input_event_id="event-7",
            input_sequence_no=7,
            expected_state_version=4,
            expected_cursor=6,
            expected_binding_version=2,
            rejection_event=rejection_event,
            operation_terminal_state=ControlOperationStatus.FAILED,
        )
    )


def make_receipt() -> ControlApplyReceipt:
    return ControlApplyReceipt(
        game_id="game-1",
        session_id="session-1",
        committed_state_version=4,
        committed_cursor=7,
        result_event_ids=("event-rejected-1",),
        operation_status=ControlOperationStatus.FAILED,
        ownership_generation=3,
        commit_evidence_reference="commit-evidence-1",
    )


def test_validated_turn_evidence_is_immutable_and_freezes_collections() -> None:
    evidence = make_validated_evidence()

    assert isinstance(evidence.participant_views, tuple)
    assert not hasattr(evidence, "__dict__")
    with pytest.raises(FrozenInstanceError):
        evidence.session_view = replace(evidence.session_view, state_version=5)  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        evidence.claim_attempt.claimed_at = NOW  # type: ignore[misc]


def test_claim_attempt_identity_is_deterministic_and_scope_bound() -> None:
    envelope = make_envelope()
    first = ControlClaimAttemptEvidence.from_envelope(envelope, claimed_at=NOW)
    second = ControlClaimAttemptEvidence.from_envelope(envelope, claimed_at=NOW)
    canonical = [
        "CONTROL_OPERATION_CLAIM_V1",
        "game-1",
        "session-1",
        "command-1",
        "operation-1",
        "event-7",
    ]
    digest = hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    assert first == second
    assert first.claim_id == f"control-claim-v1:{digest}"
    assert first.claim_id == derive_control_claim_id(
        game_id="game-1",
        session_id="session-1",
        command_id="command-1",
        operation_id="operation-1",
        input_event_id="event-7",
    )
    assert first.claim_id != derive_control_claim_id(
        game_id="game-1",
        session_id="session-1",
        command_id="command-1",
        operation_id="operation-2",
        input_event_id="event-7",
    )


def test_validated_turn_rejects_cross_evidence_mismatch_fail_closed() -> None:
    evidence = make_validated_evidence()
    stale_view = replace(evidence.session_view, state_version=5)

    with pytest.raises(CoordinatorEvidenceError):
        replace(evidence, session_view=stale_view)
    with pytest.raises(CoordinatorEvidenceError):
        replace(evidence, authorization_reference="authorization-other")


def test_coordinator_outcomes_are_closed_typed_immutable_values() -> None:
    claim = make_claim()
    rejection = make_build_reject()
    non_commit = BuildNonCommit(
        reason=BuildNonCommitReason.RECOVERY_REQUIRED,
        detail_code="RECOVERY_REQUIRED",
    )
    outcomes = (
        CoordinatorCommitReturned(
            claim=claim,
            build_outcome=rejection,
            receipt=make_receipt(),
        ),
        CoordinatorClaimConflict(
            reason=ControlApplyConflictReason.OPERATION_CLAIM_MISMATCH,
        ),
        CoordinatorClaimUnknown(
            reason=CoordinatorClaimUnknownReason.CLAIM_EVIDENCE_MISMATCH,
        ),
        CoordinatorNonCommit(claim=claim, outcome=non_commit),
        CoordinatorApplyConflict(
            claim=claim,
            build_outcome=rejection,
            reason=ControlApplyConflictReason.STATE_VERSION_MISMATCH,
        ),
        CoordinatorApplyUnknown(
            claim=claim,
            build_outcome=rejection,
            reason=CoordinatorApplyUnknownReason.RECEIPT_MISSING,
        ),
    )

    assert len({type(outcome) for outcome in outcomes}) == 6
    for outcome in outcomes:
        assert not hasattr(outcome, "__dict__")
        first_field = fields(outcome)[0]
        with pytest.raises(FrozenInstanceError):
            setattr(outcome, first_field.name, getattr(outcome, first_field.name))


def test_evidence_contracts_contain_no_mutable_runtime_references() -> None:
    forbidden = ("actor", "mailbox", "task", "repository", "port", "callback")
    for value_type in (
        ActorValidatedControlTurnEvidence,
        ControlClaimAttemptEvidence,
        CoordinatorCommitReturned,
    ):
        for field in fields(value_type):
            contract = f"{field.name} {field.type}".lower()
            assert not any(term in contract for term in forbidden)

    module_path = (
        Path(__file__).parents[2]
        / "game_runtime"
        / "session_control"
        / "coordinator_evidence.py"
    )
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported_modules = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not any(
        forbidden_module in module
        for module in imported_modules
        for forbidden_module in ("actor", "interfaces", "persistence", "repository")
    )


def test_coordinator_has_no_state_writer_bypass() -> None:
    module_path = (
        Path(__file__).parents[2]
        / "game_runtime"
        / "session_control"
        / "apply_coordinator.py"
    )
    source = module_path.read_text(encoding="utf-8").lower()

    assert "update_state" not in source
    assert "swap" not in source
    assert "notify" not in source
