"""Pure command dispatcher for composite Game ApplyPlan builders."""

from __future__ import annotations

from game_runtime.session_control.apply_plan_builder import (
    BuildNonCommit,
    BuildNonCommitReason,
    BuildOutcome,
)
from game_runtime.session_control.build_context import (
    CanonicalControlCommandIntent,
    ControlApplyBuildContext,
)
from game_runtime.session_control.commands import SessionCommandType
from game_runtime.session_control.composite_lifecycle_builder import (
    CompositeLifecycleControlApplyPlanBuilder,
)
from game_runtime.session_control.phase_control_builder import (
    PhaseControlApplyPlanBuilder,
)


class CompositeGameControlApplyPlanBuilder:
    """Route supported commands to their composite-native plan builder."""

    __slots__ = ()

    def build(self, context: ControlApplyBuildContext) -> BuildOutcome:
        if not isinstance(context, ControlApplyBuildContext):
            return BuildNonCommit(
                reason=BuildNonCommitReason.INVALID_CONTEXT,
                detail_code="CONTROL_APPLY_CONTEXT_TYPE_INVALID",
            )

        command_intent = getattr(context, "command_intent", None)
        if not isinstance(command_intent, CanonicalControlCommandIntent):
            return BuildNonCommit(
                reason=BuildNonCommitReason.INVALID_CONTEXT,
                detail_code="CONTROL_COMMAND_INTENT_INVALID",
            )
        command_type = getattr(command_intent, "command_type", None)
        if not isinstance(command_type, SessionCommandType):
            return BuildNonCommit(
                reason=BuildNonCommitReason.INVALID_CONTEXT,
                detail_code="CONTROL_COMMAND_TYPE_INVALID",
            )
        if command_type in {
            SessionCommandType.START_GAME,
            SessionCommandType.PAUSE_GAME,
            SessionCommandType.END_GAME,
        }:
            return CompositeLifecycleControlApplyPlanBuilder().build(context)
        if command_type is SessionCommandType.CHANGE_PHASE:
            return PhaseControlApplyPlanBuilder().build(context)
        return BuildNonCommit(
            reason=BuildNonCommitReason.REDUCER_UNAVAILABLE,
            detail_code=f"{command_type.value}_REDUCER_UNAVAILABLE",
        )
