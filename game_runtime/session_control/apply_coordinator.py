"""Actor-owned orchestration for one validated Session Control turn."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias

from game_runtime.interfaces.ports import SessionControlApplyPort
from game_runtime.session_control.apply_contract import (
    ControlApplyConflict,
    ControlApplyConflictReason,
    ControlApplyReceipt,
    ControlApplyStorageFailure,
    ControlOperationClaim,
)
from game_runtime.session_control.apply_plan_builder import (
    BuildNonCommit,
    BuildPlanReady,
    BuildReject,
    ControlApplyPlanBuilder,
)
from game_runtime.session_control.build_context import (
    ControlApplyBuildContext,
    ControlResultEventSeed,
)
from game_runtime.session_control.claim_bridge import (
    OperationClaimBridge,
    OperationClaimConflict,
    OperationClaimUnknown,
)
from game_runtime.session_control.coordinator_evidence import (
    ActorValidatedControlTurnEvidence,
    CoordinatorCommitReturned,
)


class CoordinatorClaimUnknownReason(str, Enum):
    STORAGE_FAILURE = "STORAGE_FAILURE"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    CLAIM_EVIDENCE_MISMATCH = "CLAIM_EVIDENCE_MISMATCH"
    UNEXPECTED_FAILURE = "UNEXPECTED_FAILURE"


class CoordinatorApplyUnknownReason(str, Enum):
    STORAGE_FAILURE = "STORAGE_FAILURE"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    RECEIPT_MISSING = "RECEIPT_MISSING"
    RECEIPT_EVIDENCE_MISMATCH = "RECEIPT_EVIDENCE_MISMATCH"
    UNEXPECTED_FAILURE = "UNEXPECTED_FAILURE"


@dataclass(frozen=True, slots=True)
class CoordinatorClaimConflict:
    reason: ControlApplyConflictReason

    def __post_init__(self) -> None:
        _require_conflict_reason(self.reason)


@dataclass(frozen=True, slots=True)
class CoordinatorClaimUnknown:
    reason: CoordinatorClaimUnknownReason

    def __post_init__(self) -> None:
        if not isinstance(self.reason, CoordinatorClaimUnknownReason):
            raise TypeError("reason must be a CoordinatorClaimUnknownReason")


@dataclass(frozen=True, slots=True)
class CoordinatorNonCommit:
    claim: ControlOperationClaim
    outcome: BuildNonCommit

    def __post_init__(self) -> None:
        _require_claim(self.claim)
        if not isinstance(self.outcome, BuildNonCommit):
            raise TypeError("outcome must be a BuildNonCommit")


@dataclass(frozen=True, slots=True)
class CoordinatorApplyConflict:
    claim: ControlOperationClaim
    build_outcome: BuildPlanReady | BuildReject
    reason: ControlApplyConflictReason

    def __post_init__(self) -> None:
        _require_claim(self.claim)
        _require_committable_build_outcome(self.build_outcome)
        _require_conflict_reason(self.reason)


@dataclass(frozen=True, slots=True)
class CoordinatorApplyUnknown:
    claim: ControlOperationClaim
    build_outcome: BuildPlanReady | BuildReject
    reason: CoordinatorApplyUnknownReason

    def __post_init__(self) -> None:
        _require_claim(self.claim)
        _require_committable_build_outcome(self.build_outcome)
        if not isinstance(self.reason, CoordinatorApplyUnknownReason):
            raise TypeError("reason must be a CoordinatorApplyUnknownReason")


CoordinatorOutcome: TypeAlias = (
    CoordinatorCommitReturned
    | CoordinatorClaimConflict
    | CoordinatorClaimUnknown
    | CoordinatorNonCommit
    | CoordinatorApplyConflict
    | CoordinatorApplyUnknown
)


class ActorOwnedApplyCoordinator:
    """Orchestrate claim, pure build, and one Atomic Apply Port call."""

    __slots__ = ("_apply_port", "_builder", "_claim_bridge")

    def __init__(
        self,
        *,
        apply_port: SessionControlApplyPort,
        builder: ControlApplyPlanBuilder,
    ) -> None:
        if not callable(getattr(apply_port, "commit_control_apply", None)) or not callable(
            getattr(apply_port, "commit_control_rejection", None)
        ):
            raise TypeError("apply_port must define Control commit operations")
        if not callable(getattr(builder, "build", None)):
            raise TypeError("builder must define build")
        self._apply_port = apply_port
        self._builder = builder
        self._claim_bridge = OperationClaimBridge(apply_port)

    async def coordinate(
        self,
        evidence: ActorValidatedControlTurnEvidence,
    ) -> CoordinatorOutcome:
        if not isinstance(evidence, ActorValidatedControlTurnEvidence):
            raise TypeError("evidence must be ActorValidatedControlTurnEvidence")

        attempt = evidence.claim_attempt
        try:
            claim = await self._claim_bridge.acquire(
                evidence.envelope,
                attempt.claim_id,
                attempt.claimed_at,
            )
        except OperationClaimConflict as exc:
            return CoordinatorClaimConflict(reason=exc.reason)
        except OperationClaimUnknown as exc:
            return CoordinatorClaimUnknown(
                reason=CoordinatorClaimUnknownReason(exc.reason.value)
            )

        context = _build_context(evidence, claim)
        build_outcome = self._builder.build(context)
        if isinstance(build_outcome, BuildNonCommit):
            return CoordinatorNonCommit(claim=claim, outcome=build_outcome)
        if not isinstance(build_outcome, (BuildPlanReady, BuildReject)):
            raise TypeError("builder must return a typed BuildOutcome")

        try:
            receipt = await self._commit(build_outcome, claim)
        except ControlApplyConflict as exc:
            return CoordinatorApplyConflict(
                claim=claim,
                build_outcome=build_outcome,
                reason=exc.reason,
            )
        except ControlApplyStorageFailure:
            return _apply_unknown(
                claim, build_outcome, CoordinatorApplyUnknownReason.STORAGE_FAILURE
            )
        except asyncio.CancelledError:
            return _apply_unknown(
                claim, build_outcome, CoordinatorApplyUnknownReason.CANCELLED
            )
        except TimeoutError:
            return _apply_unknown(
                claim, build_outcome, CoordinatorApplyUnknownReason.TIMEOUT
            )
        except Exception:
            return _apply_unknown(
                claim, build_outcome, CoordinatorApplyUnknownReason.UNEXPECTED_FAILURE
            )

        if receipt is None:
            return _apply_unknown(
                claim, build_outcome, CoordinatorApplyUnknownReason.RECEIPT_MISSING
            )
        if not isinstance(receipt, ControlApplyReceipt):
            return _apply_unknown(
                claim,
                build_outcome,
                CoordinatorApplyUnknownReason.RECEIPT_EVIDENCE_MISMATCH,
            )
        return CoordinatorCommitReturned(
            claim=claim,
            build_outcome=build_outcome,
            receipt=receipt,
        )

    async def _commit(
        self,
        build_outcome: BuildPlanReady | BuildReject,
        claim: ControlOperationClaim,
    ) -> ControlApplyReceipt:
        if isinstance(build_outcome, BuildPlanReady):
            return await self._apply_port.commit_control_apply(
                build_outcome.plan, claim
            )
        return await self._apply_port.commit_control_rejection(
            build_outcome.plan, claim
        )


def _build_context(
    evidence: ActorValidatedControlTurnEvidence,
    claim: ControlOperationClaim,
) -> ControlApplyBuildContext:
    event = evidence.envelope.event
    return ControlApplyBuildContext(
        envelope=evidence.envelope,
        claim=claim,
        command_intent=evidence.command_intent,
        session_view=evidence.session_view,
        participant_views=evidence.participant_views,
        setup_view=evidence.setup_view,
        ownership_evidence=evidence.ownership_evidence,
        result_event_seed=ControlResultEventSeed(
            seed_contract_version=1,
            game_id=event.game_id,
            session_id=event.session_id,
            command_id=evidence.envelope.command_id,
            operation_id=evidence.envelope.operation_id,
            input_event_id=event.event_id,
            timestamp=claim.claimed_at,
            correlation_id=evidence.envelope.correlation_id,
        ),
        lifecycle_evidence=evidence.lifecycle_evidence,
        setup_participant_evidence=evidence.setup_participant_evidence,
    )


def _apply_unknown(
    claim: ControlOperationClaim,
    build_outcome: BuildPlanReady | BuildReject,
    reason: CoordinatorApplyUnknownReason,
) -> CoordinatorApplyUnknown:
    return CoordinatorApplyUnknown(
        claim=claim,
        build_outcome=build_outcome,
        reason=reason,
    )


def _require_claim(claim: object) -> None:
    if not isinstance(claim, ControlOperationClaim):
        raise TypeError("claim must be a ControlOperationClaim")


def _require_committable_build_outcome(outcome: object) -> None:
    if not isinstance(outcome, (BuildPlanReady, BuildReject)):
        raise TypeError("build_outcome must be BuildPlanReady or BuildReject")


def _require_conflict_reason(reason: object) -> None:
    if not isinstance(reason, ControlApplyConflictReason):
        raise TypeError("reason must be a ControlApplyConflictReason")
