"""Pure promotion from legacy Lifecycle plans to composite Game snapshots."""

from __future__ import annotations

from dataclasses import fields, replace

from game_runtime.session_control.apply_contract import (
    CandidateSessionSnapshot,
    ControlApplyPlan,
)
from game_runtime.session_control.apply_plan_builder import (
    BuildNonCommit,
    BuildNonCommitReason,
    BuildOutcome,
    BuildPlanReady,
)
from game_runtime.session_control.build_context import ControlApplyBuildContext
from game_runtime.session_control.composite_snapshot import (
    COMPOSITE_SNAPSHOT_SCHEMA_VERSION,
    CandidateGameSnapshot,
    CompositeSnapshotContractError,
    LifecycleSnapshotSlice,
    ParticipantSnapshotRecord,
    PhaseSnapshotSlice,
)
from game_runtime.session_control.lifecycle_phase_builder import (
    LifecycleControlApplyPlanBuilder,
)


class CompositeLifecycleControlApplyPlanBuilder:
    """Promote one successful legacy Lifecycle plan without changing its chain."""

    __slots__ = ()

    def build(self, context: ControlApplyBuildContext) -> BuildOutcome:
        outcome = LifecycleControlApplyPlanBuilder().build(context)
        if not isinstance(outcome, BuildPlanReady):
            return outcome
        return _promote_lifecycle_apply_plan(context=context, ready=outcome)


def _promote_lifecycle_apply_plan(
    *,
    context: ControlApplyBuildContext,
    ready: BuildPlanReady,
) -> BuildPlanReady | BuildNonCommit:
    """Return one complete composite plan or a closed non-commit outcome."""

    if not isinstance(context, ControlApplyBuildContext):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "COMPOSITE_PLAN_PRESERVATION_FAILED",
        )
    if not isinstance(ready, BuildPlanReady):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "COMPOSITE_PLAN_PRESERVATION_FAILED",
        )
    current = context.session_view.current_game_snapshot
    if current is None:
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "COMPOSITE_CURRENT_SNAPSHOT_MISSING",
        )
    if current.snapshot_schema_version != COMPOSITE_SNAPSHOT_SCHEMA_VERSION:
        return _noncommit(
            BuildNonCommitReason.UNKNOWN_SCHEMA,
            "COMPOSITE_SNAPSHOT_SCHEMA_UNSUPPORTED",
        )

    plan = ready.plan
    legacy_candidate = plan.candidate_snapshot
    binding_failure = _validate_plan_binding(
        context,
        plan,
        legacy_candidate,
        current,
    )
    if binding_failure is not None:
        return binding_failure
    participant_failure = _validate_participant_binding(context, current)
    if participant_failure is not None:
        return participant_failure
    setup_failure = _validate_setup_binding(context, current)
    if setup_failure is not None:
        return setup_failure

    try:
        lifecycle = LifecycleSnapshotSlice(
            schema_version=current.lifecycle.schema_version,
            domain_version=current.lifecycle.domain_version + 1,
            status=legacy_candidate.status,
        )
        phase = current.phase
        if legacy_candidate.current_phase is not current.phase.phase:
            phase = PhaseSnapshotSlice(
                schema_version=current.phase.schema_version,
                domain_version=current.phase.domain_version + 1,
                phase=legacy_candidate.current_phase,
            )
        candidate = CandidateGameSnapshot(
            game_id=current.game_id,
            session_id=current.session_id,
            group_id=current.group_id,
            dm_participant_id=current.dm_participant_id,
            status=legacy_candidate.status,
            current_phase=legacy_candidate.current_phase,
            state_version=legacy_candidate.state_version,
            last_applied_sequence_no=legacy_candidate.last_applied_sequence_no,
            snapshot_schema_version=current.snapshot_schema_version,
            lifecycle=lifecycle,
            phase=phase,
            setup=current.setup,
            participants=current.participants,
            game_rules=current.game_rules,
            quest=current.quest,
            hidden_state=current.hidden_state,
        )
        promoted_plan = replace(plan, candidate_snapshot=candidate)
    except (CompositeSnapshotContractError, TypeError, ValueError):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "COMPOSITE_CANDIDATE_INVALID",
        )

    if not _plan_is_preserved(plan, promoted_plan):
        return _noncommit(
            BuildNonCommitReason.INVALID_CONTEXT,
            "COMPOSITE_PLAN_PRESERVATION_FAILED",
        )
    return BuildPlanReady(plan=promoted_plan)


def _validate_plan_binding(
    context: ControlApplyBuildContext,
    plan: ControlApplyPlan,
    legacy_candidate: CandidateSessionSnapshot,
    current: CandidateGameSnapshot,
) -> BuildNonCommit | None:
    session = context.session_view
    event = context.envelope.event
    scope = (
        session.game_id,
        session.session_id,
        session.group_id,
        session.dm_participant_id,
    )
    if (
        current.game_id,
        current.session_id,
        current.group_id,
        current.dm_participant_id,
    ) != scope or (
        plan.game_id,
        plan.session_id,
        plan.group_id,
        legacy_candidate.dm_participant_id,
    ) != scope or (event.game_id, event.session_id) != scope[:2]:
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "COMPOSITE_SCOPE_BINDING_MISMATCH",
        )
    if (
        current.status is not session.status
        or current.current_phase is not session.current_phase
        or current.lifecycle.status is not session.status
        or current.phase.phase is not session.current_phase
    ):
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "COMPOSITE_LIFECYCLE_BINDING_MISMATCH",
        )
    if (
        current.state_version != session.state_version
        or plan.expected_state_version != session.state_version
        or legacy_candidate.state_version != current.state_version + 1
    ):
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "COMPOSITE_STATE_VERSION_MISMATCH",
        )
    if (
        current.last_applied_sequence_no > session.last_applied_sequence_no
        or plan.expected_cursor != session.last_applied_sequence_no
        or plan.input_sequence_no != session.last_applied_sequence_no + 1
        or legacy_candidate.last_applied_sequence_no
        != plan.input_sequence_no
    ):
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "COMPOSITE_CURSOR_BINDING_MISMATCH",
        )
    return None


def _validate_participant_binding(
    context: ControlApplyBuildContext,
    current: CandidateGameSnapshot,
) -> BuildNonCommit | None:
    try:
        expected = tuple(
            ParticipantSnapshotRecord(
                participant_id=view.participant_id,
                participant_type=view.participant_type,
                membership_state=view.membership_state,
                character_id=view.character_id,
                binding_version=view.binding_version,
            )
            for view in sorted(
                context.participant_views,
                key=lambda view: view.participant_id,
            )
        )
    except (CompositeSnapshotContractError, TypeError, ValueError):
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "COMPOSITE_PARTICIPANT_BINDING_MISMATCH",
        )
    if current.participants.participants != expected:
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "COMPOSITE_PARTICIPANT_BINDING_MISMATCH",
        )
    return None


def _validate_setup_binding(
    context: ControlApplyBuildContext,
    current: CandidateGameSnapshot,
) -> BuildNonCommit | None:
    setup = current.setup
    view = context.setup_view
    if setup.domain_version == 0:
        matches = (
            view is None
            and setup.script_id is None
            and setup.public_name is None
            and setup.manifest_reference is None
            and setup.manifest_version is None
        )
    else:
        matches = (
            view is not None
            and view.script_id == setup.script_id
            and view.public_name == setup.public_name
            and view.manifest_reference == setup.manifest_reference
            and view.setup_version == setup.domain_version
        )
    if not matches:
        return _noncommit(
            BuildNonCommitReason.EVIDENCE_MISMATCH,
            "COMPOSITE_SETUP_BINDING_MISMATCH",
        )
    return None


def _plan_is_preserved(
    original: ControlApplyPlan,
    promoted: ControlApplyPlan,
) -> bool:
    return all(
        getattr(original, field.name) == getattr(promoted, field.name)
        for field in fields(ControlApplyPlan)
        if field.name != "candidate_snapshot"
    )


def _noncommit(
    reason: BuildNonCommitReason,
    detail_code: str,
) -> BuildNonCommit:
    return BuildNonCommit(reason=reason, detail_code=detail_code)
