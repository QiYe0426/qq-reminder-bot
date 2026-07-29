from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timezone
import hashlib
import json

import pytest

from game_runtime.event import (
    EventVisibility,
    GameEvent,
    GameEventSource,
    GameEventType,
)
from game_runtime.participant import ParticipantMembershipState, ParticipantType
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control.apply_contract import ControlOperationClaim
from game_runtime.session_control.apply_plan_builder import (
    BuildNonCommit,
    BuildNonCommitReason,
    BuildPlanReady,
    BuildReject,
    ControlApplyPlanBuilder,
)
from game_runtime.session_control.build_context import (
    CanonicalControlCommandIntent,
    ControlApplyBuildContext,
    ControlApplyBuildContextError,
    ControlOwnershipBuildEvidence,
    ControlParticipantBuildView,
    ControlResultEventSeed,
    ControlSessionBuildView,
    ControlSetupBuildView,
)
from game_runtime.session_control.commands import (
    CreateSessionPayload,
    PauseGamePayload,
    SessionCommandType,
)
from game_runtime.session_control.confirmation import fingerprint_payload
from game_runtime.session_control.delivery import ControlEventDeliveryEnvelope
from game_runtime.session_control.event_integration import DMCommandEventPayload


NOW = datetime(2026, 7, 16, 8, 30, tzinfo=timezone.utc)


def _make_context() -> ControlApplyBuildContext:
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
    claim = ControlOperationClaim(
        game_id="game-1",
        session_id="session-1",
        command_id="command-1",
        operation_id="operation-1",
        input_event_id="event-7",
        claim_id="claim-1",
        claimed_at=NOW,
    )
    intent = CanonicalControlCommandIntent(
        intent_schema_version=1,
        command_type=SessionCommandType.PAUSE_GAME,
        payload=payload,
        payload_fingerprint=payload_fingerprint,
    )
    session_view = ControlSessionBuildView(
        game_id="game-1",
        session_id="session-1",
        group_id="group-1",
        dm_participant_id="participant-dm",
        status=GameSessionStatus.RUNNING,
        current_phase=GamePhase.EXPLORATION,
        state_version=4,
        last_applied_sequence_no=6,
    )
    participant_view = ControlParticipantBuildView(
        game_id="game-1",
        session_id="session-1",
        participant_id="participant-dm",
        participant_type=ParticipantType.DM,
        membership_state=ParticipantMembershipState.ACTIVE,
        character_id=None,
        binding_version=2,
    )
    setup_view = ControlSetupBuildView(
        game_id="game-1",
        session_id="session-1",
        script_id="script-1",
        public_name="Public Script",
        manifest_reference="manifest-1",
        setup_version=1,
    )
    ownership = ControlOwnershipBuildEvidence(
        game_id="game-1",
        session_id="session-1",
        group_id="group-1",
        active_generation=3,
        last_allocated_generation=3,
        observed_state_version=4,
    )
    seed = ControlResultEventSeed(
        seed_contract_version=1,
        game_id="game-1",
        session_id="session-1",
        command_id="command-1",
        operation_id="operation-1",
        input_event_id="event-7",
        timestamp=NOW,
        correlation_id="correlation-1",
    )
    return ControlApplyBuildContext(
        envelope=envelope,
        claim=claim,
        command_intent=intent,
        session_view=session_view,
        participant_views=[participant_view],
        setup_view=setup_view,
        ownership_evidence=ownership,
        result_event_seed=seed,
    )


def test_build_context_is_deeply_value_based_and_immutable() -> None:
    context = _make_context()

    assert isinstance(context.participant_views, tuple)
    assert not hasattr(context, "__dict__")
    with pytest.raises(FrozenInstanceError):
        context.session_view = replace(context.session_view, state_version=5)  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        context.command_intent.payload_fingerprint = "0" * 64  # type: ignore[misc]


def test_build_context_contract_has_no_runtime_service_references() -> None:
    value_types = (
        ControlApplyBuildContext,
        CanonicalControlCommandIntent,
        ControlSessionBuildView,
        ControlParticipantBuildView,
        ControlSetupBuildView,
        ControlOwnershipBuildEvidence,
        ControlResultEventSeed,
    )
    forbidden = ("repository", "port", "callback", "task", "loader")

    for value_type in value_types:
        for field in fields(value_type):
            field_contract = f"{field.name} {field.type}".lower()
            assert not any(term in field_contract for term in forbidden)


def test_builder_is_a_structural_interface_only() -> None:
    class FakeBuilder:
        def build(self, context: ControlApplyBuildContext) -> BuildNonCommit:
            assert context == _make_context()
            return BuildNonCommit(
                reason=BuildNonCommitReason.REDUCER_UNAVAILABLE,
                detail_code="P3_D_6_6_NOT_AVAILABLE",
            )

    builder = FakeBuilder()

    assert isinstance(builder, ControlApplyPlanBuilder)
    assert isinstance(builder.build(_make_context()), BuildNonCommit)


def test_typed_outcomes_are_closed_immutable_contracts() -> None:
    outcome = BuildNonCommit(
        reason=BuildNonCommitReason.INVALID_CONTEXT,
        detail_code="INVALID_CONTEXT",
    )

    with pytest.raises(FrozenInstanceError):
        outcome.detail_code = "CHANGED"  # type: ignore[misc]
    with pytest.raises(TypeError):
        BuildPlanReady(plan=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        BuildReject(plan=object())  # type: ignore[arg-type]


def test_result_event_seed_derives_stable_identity_without_clock_or_randomness() -> None:
    seed = _make_context().result_event_seed
    canonical = [
        "CONTROL_RESULT_EVENT_ID_V1",
        "game-1",
        "session-1",
        "command-1",
        "operation-1",
        "event-7",
        GameEventType.SESSION_PAUSED.value,
        1,
    ]
    expected_digest = hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    first = seed.derive_event_id(GameEventType.SESSION_PAUSED, ordinal=1)
    second = seed.derive_event_id(GameEventType.SESSION_PAUSED, ordinal=1)

    assert first == second == f"control-result-v1:{expected_digest}"
    assert seed.derive_event_id(GameEventType.SESSION_PAUSED, ordinal=2) != first
    assert seed.derive_event_id(GameEventType.SESSION_RESUMED, ordinal=1) != first


def test_result_event_seed_rejects_non_result_event_and_invalid_ordinal() -> None:
    seed = _make_context().result_event_seed

    with pytest.raises(ControlApplyBuildContextError):
        seed.derive_event_id(GameEventType.DM_COMMAND, ordinal=1)
    with pytest.raises(ControlApplyBuildContextError):
        seed.derive_event_id(GameEventType.SESSION_PAUSED, ordinal=0)


@pytest.mark.parametrize(
    "field_name, replacement",
    [
        ("claim", {"operation_id": "other-operation"}),
        ("session_view", {"state_version": 5}),
        ("ownership_evidence", {"observed_state_version": 5}),
        ("result_event_seed", {"correlation_id": "other-correlation"}),
    ],
)
def test_context_rejects_cross_evidence_binding_mismatch(
    field_name: str,
    replacement: dict[str, object],
) -> None:
    context = _make_context()
    bad_value = replace(getattr(context, field_name), **replacement)

    with pytest.raises(ControlApplyBuildContextError):
        replace(context, **{field_name: bad_value})


def test_context_rejects_requester_binding_mismatch() -> None:
    context = _make_context()
    bad_participant = replace(context.participant_views[0], binding_version=3)

    with pytest.raises(ControlApplyBuildContextError):
        replace(context, participant_views=(bad_participant,))


def test_context_rejects_sequence_gap_fail_closed() -> None:
    context = _make_context()
    stale_view = replace(context.session_view, last_applied_sequence_no=5)

    with pytest.raises(ControlApplyBuildContextError):
        replace(context, session_view=stale_view)


def test_canonical_intent_rejects_payload_fingerprint_mismatch() -> None:
    context = _make_context()

    with pytest.raises(ControlApplyBuildContextError):
        replace(context.command_intent, payload_fingerprint="0" * 64)


def test_canonical_intent_rejects_create_session_path() -> None:
    payload = CreateSessionPayload(prospective_dm_id="participant-dm")

    with pytest.raises(ControlApplyBuildContextError):
        CanonicalControlCommandIntent(
            intent_schema_version=1,
            command_type=SessionCommandType.CREATE_SESSION,
            payload=payload,
            payload_fingerprint=fingerprint_payload(payload),
        )
