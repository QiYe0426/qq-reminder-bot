"""Immutable Actor-visible composite Game Runtime state contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from game_runtime.session_control.composite_snapshot import (
    CandidateGameSnapshot,
    CompositeSnapshotContractError,
    CompositeSnapshotFailureReason,
)


class ControlCompletionKind(str, Enum):
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class ControlCompletionIdentity:
    game_id: str
    session_id: str
    command_id: str
    operation_id: str
    input_event_id: str
    input_sequence_no: int
    state_version: int
    commit_evidence_reference: str
    completion_kind: ControlCompletionKind

    def __post_init__(self) -> None:
        for name in (
            "game_id",
            "session_id",
            "command_id",
            "operation_id",
            "input_event_id",
            "commit_evidence_reference",
        ):
            _require_text(name, getattr(self, name))
        _require_non_negative_int("input_sequence_no", self.input_sequence_no)
        if self.input_sequence_no == 0:
            _fail(CompositeSnapshotFailureReason.INVALID_CONTROL_CURSOR)
        _require_non_negative_int("state_version", self.state_version)
        if not isinstance(self.completion_kind, ControlCompletionKind):
            raise TypeError("completion_kind must be a ControlCompletionKind")


@dataclass(frozen=True, slots=True)
class ActorVisibleGameState:
    snapshot: CandidateGameSnapshot
    committed_control_cursor: int
    ownership_generation: int | None
    last_completion_identity: ControlCompletionIdentity | None

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, CandidateGameSnapshot):
            raise TypeError("snapshot must be a CandidateGameSnapshot")
        _require_non_negative_int(
            "committed_control_cursor",
            self.committed_control_cursor,
        )
        if self.ownership_generation is not None:
            _require_non_negative_int(
                "ownership_generation",
                self.ownership_generation,
            )
            if self.ownership_generation == 0:
                _fail(CompositeSnapshotFailureReason.INVALID_SLICE_VALUE)
        completion = self.last_completion_identity
        if completion is not None and not isinstance(
            completion,
            ControlCompletionIdentity,
        ):
            raise TypeError(
                "last_completion_identity must be a ControlCompletionIdentity"
            )

        snapshot_cursor = self.snapshot.last_applied_sequence_no
        if self.committed_control_cursor < snapshot_cursor:
            _fail(CompositeSnapshotFailureReason.INVALID_CONTROL_CURSOR)
        if completion is None:
            if self.committed_control_cursor != snapshot_cursor:
                _fail(
                    CompositeSnapshotFailureReason.COMPLETION_IDENTITY_MISMATCH
                )
            return
        if (
            completion.game_id != self.snapshot.game_id
            or completion.session_id != self.snapshot.session_id
        ):
            _fail(CompositeSnapshotFailureReason.SCOPE_MISMATCH)
        if (
            completion.input_sequence_no != self.committed_control_cursor
            or completion.state_version != self.snapshot.state_version
        ):
            _fail(CompositeSnapshotFailureReason.COMPLETION_IDENTITY_MISMATCH)
        if (
            completion.completion_kind is ControlCompletionKind.APPLIED
            and self.committed_control_cursor != snapshot_cursor
        ):
            _fail(CompositeSnapshotFailureReason.INVALID_CONTROL_CURSOR)
        if (
            completion.completion_kind is ControlCompletionKind.REJECTED
            and self.committed_control_cursor <= snapshot_cursor
        ):
            _fail(CompositeSnapshotFailureReason.INVALID_CONTROL_CURSOR)


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text")
    if not value.strip():
        _fail(CompositeSnapshotFailureReason.INVALID_SLICE_VALUE)


def _require_non_negative_int(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        reason = (
            CompositeSnapshotFailureReason.INVALID_CONTROL_CURSOR
            if name in {"input_sequence_no", "committed_control_cursor"}
            else CompositeSnapshotFailureReason.INVALID_SLICE_VALUE
        )
        _fail(reason)


def _fail(reason: CompositeSnapshotFailureReason) -> None:
    raise CompositeSnapshotContractError(reason)
