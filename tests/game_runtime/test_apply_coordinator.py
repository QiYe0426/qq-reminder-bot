from __future__ import annotations

import ast
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from game_runtime.event import EventVisibility, GameEvent, GameEventSource, GameEventType
from game_runtime.participant import ParticipantMembershipState, ParticipantType
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control.apply_contract import (
    ControlApplyConflict,
    ControlApplyConflictReason,
    ControlApplyReceipt,
    ControlApplyStorageFailure,
    ControlOperationClaim,
)
from game_runtime.session_control.apply_coordinator import (
    ActorOwnedApplyCoordinator,
    CoordinatorApplyConflict,
    CoordinatorApplyUnknown,
    CoordinatorApplyUnknownReason,
    CoordinatorClaimConflict,
    CoordinatorClaimUnknown,
    CoordinatorCommitReturned,
    CoordinatorNonCommit,
)
from game_runtime.session_control.apply_plan_builder import (
    BuildNonCommit,
    BuildNonCommitReason,
    BuildPlanReady,
    BuildReject,
)
from game_runtime.session_control.build_context import (
    CanonicalControlCommandIntent,
    ControlApplyBuildContext,
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
)
from game_runtime.session_control.delivery import ControlEventDeliveryEnvelope
from game_runtime.session_control.event_integration import DMCommandEventPayload
from game_runtime.session_control.lifecycle_evidence import ControlLifecycleApplyEvidence
from game_runtime.session_control.lifecycle_phase_builder import (
    LifecyclePhaseControlApplyPlanBuilder,
)
from game_runtime.session_control.operation import ControlOperationStatus
from game_runtime.session_control.setup_participant_evidence import (
    ControlSetupParticipantApplyEvidence,
)


NOW = datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def make_evidence(
    *, status: GameSessionStatus = GameSessionStatus.RUNNING
) -> ActorValidatedControlTurnEvidence:
    command_payload = PauseGamePayload(reason_code="DM_REQUEST")
    payload_fingerprint = fingerprint_payload(command_payload)
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
    return ActorValidatedControlTurnEvidence(
        envelope=envelope,
        command_intent=CanonicalControlCommandIntent(
            intent_schema_version=1,
            command_type=SessionCommandType.PAUSE_GAME,
            payload=command_payload,
            payload_fingerprint=payload_fingerprint,
        ),
        session_view=ControlSessionBuildView(
            game_id="game-1",
            session_id="session-1",
            group_id="group-1",
            dm_participant_id="participant-dm",
            status=status,
            current_phase=GamePhase.EXPLORATION,
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
            envelope, claimed_at=NOW
        ),
    )


class ScriptedBuilder:
    def __init__(self, outcome: BuildNonCommit) -> None:
        self.outcome = outcome
        self.contexts: list[ControlApplyBuildContext] = []

    def build(self, context: ControlApplyBuildContext) -> BuildNonCommit:
        self.contexts.append(context)
        return self.outcome


class CapturingBuilder:
    def __init__(self) -> None:
        self.delegate = LifecyclePhaseControlApplyPlanBuilder()
        self.contexts: list[ControlApplyBuildContext] = []

    def build(self, context: ControlApplyBuildContext):
        self.contexts.append(context)
        return self.delegate.build(context)


class FakeApplyPort:
    def __init__(
        self,
        *,
        claim_failure: BaseException | None = None,
        commit_failure: BaseException | None = None,
    ) -> None:
        self.claim_failure = claim_failure
        self.commit_failure = commit_failure
        self.calls: list[tuple[str, object]] = []
        self.last_receipt: ControlApplyReceipt | None = None

    async def claim_operation(self, **values: object) -> ControlOperationClaim:
        self.calls.append(("claim_operation", dict(values)))
        if self.claim_failure is not None:
            raise self.claim_failure
        return ControlOperationClaim(**values)  # type: ignore[arg-type]

    async def commit_control_apply(self, plan, claim) -> ControlApplyReceipt:
        self.calls.append(("commit_control_apply", (plan, claim)))
        if self.commit_failure is not None:
            raise self.commit_failure
        return self._receipt(plan, claim, ControlOperationStatus.SUCCESS)

    async def commit_control_rejection(self, plan, claim) -> ControlApplyReceipt:
        self.calls.append(("commit_control_rejection", (plan, claim)))
        if self.commit_failure is not None:
            raise self.commit_failure
        return self._receipt(plan, claim, ControlOperationStatus.FAILED)

    def _receipt(self, plan, claim, status) -> ControlApplyReceipt:
        event = (
            plan.result_events[0]
            if status is ControlOperationStatus.SUCCESS
            else plan.rejection_event
        )
        committed_version = (
            plan.candidate_snapshot.state_version
            if status is ControlOperationStatus.SUCCESS
            else plan.expected_state_version
        )
        receipt = ControlApplyReceipt(
            game_id=claim.game_id,
            session_id=claim.session_id,
            committed_state_version=committed_version,
            committed_cursor=plan.input_sequence_no,
            result_event_ids=(event.event_id,),
            operation_status=status,
            ownership_generation=3,
            commit_evidence_reference="commit-evidence-1",
        )
        self.last_receipt = receipt
        return receipt


def coordinator(port: FakeApplyPort, builder=LifecyclePhaseControlApplyPlanBuilder()):
    return ActorOwnedApplyCoordinator(apply_port=port, builder=builder)


def test_claim_success_builds_context_and_commits_apply() -> None:
    evidence = make_evidence()
    port = FakeApplyPort()
    builder = CapturingBuilder()
    outcome = run(coordinator(port, builder).coordinate(evidence))

    assert isinstance(outcome, CoordinatorCommitReturned)
    assert isinstance(outcome.build_outcome, BuildPlanReady)
    assert [name for name, _ in port.calls] == [
        "claim_operation",
        "commit_control_apply",
    ]
    assert outcome.claim.claim_id == evidence.claim_attempt.claim_id
    assert outcome.claim.claimed_at == evidence.claim_attempt.claimed_at
    context = builder.contexts[0]
    assert context.envelope is evidence.envelope
    assert context.command_intent is evidence.command_intent
    assert context.lifecycle_evidence is evidence.lifecycle_evidence
    assert context.setup_participant_evidence is evidence.setup_participant_evidence
    assert context.result_event_seed.timestamp == outcome.claim.claimed_at
    assert context.result_event_seed.correlation_id == evidence.envelope.correlation_id


def test_build_reject_is_committed_through_rejection_port() -> None:
    port = FakeApplyPort()
    outcome = run(
        coordinator(port).coordinate(make_evidence(status=GameSessionStatus.PAUSED))
    )

    assert isinstance(outcome, CoordinatorCommitReturned)
    assert isinstance(outcome.build_outcome, BuildReject)
    assert [name for name, _ in port.calls] == [
        "claim_operation",
        "commit_control_rejection",
    ]


def test_build_noncommit_stops_without_commit() -> None:
    noncommit = BuildNonCommit(
        reason=BuildNonCommitReason.RECOVERY_REQUIRED,
        detail_code="RECOVERY_REQUIRED",
    )
    builder = ScriptedBuilder(noncommit)
    port = FakeApplyPort()
    outcome = run(coordinator(port, builder).coordinate(make_evidence()))

    assert isinstance(outcome, CoordinatorNonCommit)
    assert outcome.outcome is noncommit
    assert [name for name, _ in port.calls] == ["claim_operation"]


def test_claim_conflict_stops_before_build_and_commit() -> None:
    port = FakeApplyPort(
        claim_failure=ControlApplyConflict(
            ControlApplyConflictReason.OPERATION_CLAIM_MISMATCH
        )
    )
    outcome = run(coordinator(port).coordinate(make_evidence()))

    assert isinstance(outcome, CoordinatorClaimConflict)
    assert [name for name, _ in port.calls] == ["claim_operation"]


def test_claim_unknown_stops_without_retry() -> None:
    port = FakeApplyPort(
        claim_failure=ControlApplyStorageFailure("claim unavailable")
    )
    outcome = run(coordinator(port).coordinate(make_evidence()))

    assert isinstance(outcome, CoordinatorClaimUnknown)
    assert [name for name, _ in port.calls] == ["claim_operation"]


def test_commit_unknown_is_typed_and_not_retried() -> None:
    port = FakeApplyPort(
        commit_failure=ControlApplyStorageFailure("commit unavailable")
    )
    outcome = run(coordinator(port).coordinate(make_evidence()))

    assert isinstance(outcome, CoordinatorApplyUnknown)
    assert outcome.reason is CoordinatorApplyUnknownReason.STORAGE_FAILURE
    assert [name for name, _ in port.calls] == [
        "claim_operation",
        "commit_control_apply",
    ]


def test_commit_conflict_preserves_evidence_and_is_not_retried() -> None:
    port = FakeApplyPort(
        commit_failure=ControlApplyConflict(
            ControlApplyConflictReason.STATE_VERSION_MISMATCH
        )
    )
    outcome = run(coordinator(port).coordinate(make_evidence()))

    assert isinstance(outcome, CoordinatorApplyConflict)
    assert outcome.reason is ControlApplyConflictReason.STATE_VERSION_MISMATCH
    assert isinstance(outcome.build_outcome, BuildPlanReady)
    assert outcome.claim.operation_id == "operation-1"
    assert [name for name, _ in port.calls] == [
        "claim_operation",
        "commit_control_apply",
    ]


def test_receipt_is_returned_unvalidated_for_actor_owned_validation() -> None:
    port = FakeApplyPort()
    outcome = run(coordinator(port).coordinate(make_evidence()))

    assert isinstance(outcome, CoordinatorCommitReturned)
    assert outcome.receipt is port.last_receipt


def test_coordinator_has_no_repository_or_state_writer_bypass() -> None:
    module_path = (
        Path(__file__).parents[2]
        / "game_runtime"
        / "session_control"
        / "apply_coordinator.py"
    )
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported_modules = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not any(
        forbidden in module
        for module in imported_modules
        for forbidden in ("repository", "persistence", "sqlite", "actor")
    )
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not calls.intersection(
        {"append_event", "update_state", "swap", "notify", "write_recovery"}
    )


def test_coordinator_does_not_modify_validated_evidence() -> None:
    evidence = make_evidence()
    original = replace(evidence)
    run(coordinator(FakeApplyPort()).coordinate(evidence))
    assert evidence == original
