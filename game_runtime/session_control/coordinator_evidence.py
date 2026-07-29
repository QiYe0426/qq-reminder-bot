"""Immutable evidence exchanged at the Actor-owned Coordinator boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json

from game_runtime.participant import ParticipantMembershipState
from game_runtime.session_control.apply_contract import (
    ControlApplyReceipt,
    ControlOperationClaim,
)
from game_runtime.session_control.apply_plan_builder import BuildPlanReady, BuildReject
from game_runtime.session_control.build_context import (
    CanonicalControlCommandIntent,
    ControlOwnershipBuildEvidence,
    ControlParticipantBuildView,
    ControlSessionBuildView,
    ControlSetupBuildView,
)
from game_runtime.session_control.delivery import ControlEventDeliveryEnvelope
from game_runtime.session_control.event_integration import validate_dm_command_event
from game_runtime.session_control.lifecycle_evidence import (
    ControlLifecycleApplyEvidence,
    ControlLifecycleEvidenceError,
)
from game_runtime.session_control.setup_participant_evidence import (
    ControlSetupParticipantApplyEvidence,
    ControlSetupParticipantEvidenceError,
)


class CoordinatorEvidenceError(ValueError):
    """Raised when Coordinator boundary evidence is not internally bound."""


def derive_control_claim_id(
    *,
    game_id: str,
    session_id: str,
    command_id: str,
    operation_id: str,
    input_event_id: str,
) -> str:
    """Derive the single stable claim identity for one Control Operation."""

    identity_parts = (
        game_id,
        session_id,
        command_id,
        operation_id,
        input_event_id,
    )
    for name, value in zip(
        (
            "game_id",
            "session_id",
            "command_id",
            "operation_id",
            "input_event_id",
        ),
        identity_parts,
    ):
        _require_text(name, value)
    canonical = ["CONTROL_OPERATION_CLAIM_V1", *identity_parts]
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"control-claim-v1:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class ControlClaimAttemptEvidence:
    """One immutable claim attempt created at an authoritative Actor turn."""

    claim_contract_version: int
    game_id: str
    session_id: str
    command_id: str
    operation_id: str
    input_event_id: str
    claim_id: str
    claimed_at: datetime

    def __post_init__(self) -> None:
        if self.claim_contract_version != 1:
            raise CoordinatorEvidenceError(
                "unsupported claim identity contract version"
            )
        expected_claim_id = derive_control_claim_id(
            game_id=self.game_id,
            session_id=self.session_id,
            command_id=self.command_id,
            operation_id=self.operation_id,
            input_event_id=self.input_event_id,
        )
        _require_text("claim_id", self.claim_id)
        if self.claim_id != expected_claim_id:
            raise CoordinatorEvidenceError(
                "claim_id must match deterministic Operation evidence"
            )
        _require_aware_time("claimed_at", self.claimed_at)

    @classmethod
    def from_envelope(
        cls,
        envelope: ControlEventDeliveryEnvelope,
        *,
        claimed_at: datetime,
    ) -> ControlClaimAttemptEvidence:
        """Capture one claim attempt after the Actor has validated its turn."""

        if not isinstance(envelope, ControlEventDeliveryEnvelope):
            raise TypeError("envelope must be a ControlEventDeliveryEnvelope")
        event = envelope.event
        return cls(
            claim_contract_version=1,
            game_id=event.game_id,
            session_id=event.session_id,
            command_id=envelope.command_id,
            operation_id=envelope.operation_id,
            input_event_id=event.event_id,
            claim_id=derive_control_claim_id(
                game_id=event.game_id,
                session_id=event.session_id,
                command_id=envelope.command_id,
                operation_id=envelope.operation_id,
                input_event_id=event.event_id,
            ),
            claimed_at=claimed_at,
        )


@dataclass(frozen=True, slots=True)
class ActorValidatedControlTurnEvidence:
    """Frozen values captured after Actor validation and before CAS claim."""

    envelope: ControlEventDeliveryEnvelope
    command_intent: CanonicalControlCommandIntent
    session_view: ControlSessionBuildView
    participant_views: tuple[ControlParticipantBuildView, ...]
    setup_view: ControlSetupBuildView | None
    ownership_evidence: ControlOwnershipBuildEvidence
    governance_schema_version: int
    authorization_reference: str
    confirmation_reference: str | None
    lifecycle_evidence: ControlLifecycleApplyEvidence
    setup_participant_evidence: ControlSetupParticipantApplyEvidence
    claim_attempt: ControlClaimAttemptEvidence

    def __post_init__(self) -> None:
        _require_type("envelope", self.envelope, ControlEventDeliveryEnvelope)
        _require_type(
            "command_intent",
            self.command_intent,
            CanonicalControlCommandIntent,
        )
        _require_type("session_view", self.session_view, ControlSessionBuildView)
        _require_type(
            "ownership_evidence",
            self.ownership_evidence,
            ControlOwnershipBuildEvidence,
        )
        _require_type(
            "claim_attempt", self.claim_attempt, ControlClaimAttemptEvidence
        )
        _require_type(
            "lifecycle_evidence",
            self.lifecycle_evidence,
            ControlLifecycleApplyEvidence,
        )
        _require_type(
            "setup_participant_evidence",
            self.setup_participant_evidence,
            ControlSetupParticipantApplyEvidence,
        )
        if self.setup_view is not None:
            _require_type("setup_view", self.setup_view, ControlSetupBuildView)
        _require_positive_int(
            "governance_schema_version", self.governance_schema_version
        )
        _require_text("authorization_reference", self.authorization_reference)
        if self.confirmation_reference is not None:
            _require_text("confirmation_reference", self.confirmation_reference)

        if not isinstance(self.participant_views, (tuple, list)):
            raise CoordinatorEvidenceError(
                "participant_views must be a tuple or list"
            )
        participant_views = tuple(self.participant_views)
        if any(
            not isinstance(view, ControlParticipantBuildView)
            for view in participant_views
        ):
            raise CoordinatorEvidenceError(
                "participant_views contains invalid evidence"
            )
        participant_ids = tuple(view.participant_id for view in participant_views)
        if len(participant_ids) != len(set(participant_ids)):
            raise CoordinatorEvidenceError(
                "participant_views must contain unique participant_id values"
            )
        object.__setattr__(self, "participant_views", participant_views)

        event = self.envelope.event
        payload = validate_dm_command_event(event)
        expected_scope = (event.game_id, event.session_id)
        session = self.session_view
        if (session.game_id, session.session_id) != expected_scope:
            raise CoordinatorEvidenceError(
                "Session build view scope does not match delivery evidence"
            )
        if session.state_version != self.envelope.observed_state_version:
            raise CoordinatorEvidenceError(
                "Session state version does not match delivery evidence"
            )
        if self.envelope.event_sequence_no != session.last_applied_sequence_no + 1:
            raise CoordinatorEvidenceError(
                "input Event sequence must immediately follow the Session cursor"
            )
        if (
            self.command_intent.command_type is not payload.command_type
            or self.command_intent.payload_fingerprint
            != payload.payload_fingerprint
        ):
            raise CoordinatorEvidenceError(
                "canonical command intent does not match delivery evidence"
            )

        for participant in participant_views:
            if (participant.game_id, participant.session_id) != expected_scope:
                raise CoordinatorEvidenceError(
                    "participant evidence scope does not match delivery evidence"
                )
        requester = next(
            (
                participant
                for participant in participant_views
                if participant.participant_id
                == self.envelope.requester_principal_ref
            ),
            None,
        )
        if requester is None:
            raise CoordinatorEvidenceError("requester participant evidence is missing")
        if requester.membership_state is not ParticipantMembershipState.ACTIVE:
            raise CoordinatorEvidenceError("requester participant must be ACTIVE")
        if requester.binding_version != self.envelope.requester_binding_version:
            raise CoordinatorEvidenceError(
                "requester binding does not match delivery evidence"
            )
        if session.dm_participant_id != requester.participant_id:
            raise CoordinatorEvidenceError(
                "requester must be the Session-scoped DM participant"
            )

        if self.setup_view is not None and (
            self.setup_view.game_id,
            self.setup_view.session_id,
        ) != expected_scope:
            raise CoordinatorEvidenceError(
                "setup evidence scope does not match delivery evidence"
            )
        ownership = self.ownership_evidence
        if (
            ownership.game_id,
            ownership.session_id,
            ownership.group_id,
            ownership.observed_state_version,
        ) != (
            event.game_id,
            event.session_id,
            session.group_id,
            session.state_version,
        ):
            raise CoordinatorEvidenceError(
                "ownership evidence does not match the validated Session view"
            )

        if self.authorization_reference != self.envelope.authorization_reference:
            raise CoordinatorEvidenceError(
                "authorization evidence does not match delivery evidence"
            )
        if self.confirmation_reference != self.envelope.confirmation_reference:
            raise CoordinatorEvidenceError(
                "confirmation evidence does not match delivery evidence"
            )
        try:
            self.lifecycle_evidence.validate_bindings(
                game_id=event.game_id,
                session_id=event.session_id,
                observed_state_version=session.state_version,
            )
        except ControlLifecycleEvidenceError as exc:
            raise CoordinatorEvidenceError(str(exc)) from exc
        try:
            self.setup_participant_evidence.validate_bindings(
                game_id=event.game_id,
                session_id=event.session_id,
                observed_state_version=session.state_version,
            )
        except ControlSetupParticipantEvidenceError as exc:
            raise CoordinatorEvidenceError(str(exc)) from exc
        attempt = self.claim_attempt
        if (
            attempt.game_id,
            attempt.session_id,
            attempt.command_id,
            attempt.operation_id,
            attempt.input_event_id,
        ) != (
            event.game_id,
            event.session_id,
            self.envelope.command_id,
            self.envelope.operation_id,
            event.event_id,
        ):
            raise CoordinatorEvidenceError(
                "claim attempt does not match delivery evidence"
            )


@dataclass(frozen=True, slots=True)
class CoordinatorCommitReturned:
    """Unvalidated Receipt handoff for the next Actor-owned receipt stage."""

    claim: ControlOperationClaim
    build_outcome: BuildPlanReady | BuildReject
    receipt: ControlApplyReceipt

    def __post_init__(self) -> None:
        _require_type("claim", self.claim, ControlOperationClaim)
        if not isinstance(self.build_outcome, (BuildPlanReady, BuildReject)):
            raise TypeError(
                "build_outcome must be BuildPlanReady or BuildReject"
            )
        _require_type("receipt", self.receipt, ControlApplyReceipt)


def _require_type(name: str, value: object, expected_type: type[object]) -> None:
    if not isinstance(value, expected_type):
        raise TypeError(f"{name} must be a {expected_type.__name__}")


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise CoordinatorEvidenceError(f"{name} must be non-empty text")


def _require_positive_int(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise CoordinatorEvidenceError(f"{name} must be a positive integer")


def _require_aware_time(name: str, value: object) -> None:
    if not isinstance(value, datetime):
        raise CoordinatorEvidenceError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise CoordinatorEvidenceError(f"{name} must be timezone-aware")
