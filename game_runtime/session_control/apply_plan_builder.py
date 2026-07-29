"""Contract-only boundary for future Session Control ApplyPlan reducers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, TypeAlias, runtime_checkable

from game_runtime.session_control.apply_contract import (
    ControlApplyPlan,
    ControlRejectPlan,
)
from game_runtime.session_control.build_context import ControlApplyBuildContext


class BuildNonCommitReason(str, Enum):
    """Closed reasons for returning without an Atomic Apply attempt."""

    EVIDENCE_MISMATCH = "EVIDENCE_MISMATCH"
    UNKNOWN_SCHEMA = "UNKNOWN_SCHEMA"
    INVALID_CONTEXT = "INVALID_CONTEXT"
    REDUCER_UNAVAILABLE = "REDUCER_UNAVAILABLE"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


@dataclass(frozen=True, slots=True)
class BuildPlanReady:
    """A validated success plan is ready for a future coordinator."""

    plan: ControlApplyPlan

    def __post_init__(self) -> None:
        if not isinstance(self.plan, ControlApplyPlan):
            raise TypeError("plan must be a ControlApplyPlan")


@dataclass(frozen=True, slots=True)
class BuildReject:
    """A deterministic rejection plan is ready for a future coordinator."""

    plan: ControlRejectPlan

    def __post_init__(self) -> None:
        if not isinstance(self.plan, ControlRejectPlan):
            raise TypeError("plan must be a ControlRejectPlan")


@dataclass(frozen=True, slots=True)
class BuildNonCommit:
    """Fail-closed outcome that carries no Event or mutation plan."""

    reason: BuildNonCommitReason
    detail_code: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, BuildNonCommitReason):
            raise TypeError("reason must be a BuildNonCommitReason")
        if not isinstance(self.detail_code, str) or not self.detail_code.strip():
            raise ValueError("detail_code must be non-empty text")


BuildOutcome: TypeAlias = BuildPlanReady | BuildReject | BuildNonCommit


@runtime_checkable
class ControlApplyPlanBuilder(Protocol):
    """Pure builder interface; concrete reducers are intentionally absent."""

    def build(self, context: ControlApplyBuildContext) -> BuildOutcome:
        """Transform immutable evidence into one closed typed outcome."""
