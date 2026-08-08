from __future__ import annotations

import ast
import inspect
from dataclasses import replace

import pytest

import game_runtime.session_control.composite_control_builder as dispatcher_module
from game_runtime.session import GamePhase, GameSessionStatus
from game_runtime.session_control import (
    BuildNonCommit,
    BuildNonCommitReason,
    BuildPlanReady,
    CompositeGameControlApplyPlanBuilder,
    CompositeLifecycleControlApplyPlanBuilder,
    GameRuleControlApplyPlanBuilder,
    ParticipantControlApplyPlanBuilder,
    PhaseControlApplyPlanBuilder,
    SessionCommandType,
    SetupControlApplyPlanBuilder,
)
from test_composite_lifecycle_promotion_contract import _current_snapshot
from test_lifecycle_phase_apply_plan_builder import make_context
from test_participant_control_plane_contract import _context as _participant_context
from test_setup_control_plane_contract import _context as _setup_context
from test_game_rule_control_plane_contract import _context as _game_rule_context
from test_clue_reveal_control_plane_contract import _reveal_context


def _phase_context():
    context = make_context(
        SessionCommandType.CHANGE_PHASE,
        status=GameSessionStatus.RUNNING,
        current_phase=GamePhase.EXPLORATION,
        target_phase=GamePhase.DISCUSSION,
    )
    return replace(
        context,
        session_view=replace(
            context.session_view,
            current_game_snapshot=_current_snapshot(context),
        ),
    )


def _lifecycle_context():
    context = make_context(
        SessionCommandType.START_GAME,
        status=GameSessionStatus.CREATED,
        current_phase=GamePhase.LOBBY,
    )
    return replace(
        context,
        session_view=replace(
            context.session_view,
            current_game_snapshot=_current_snapshot(context),
        ),
    )


@pytest.mark.parametrize(
    ("context_factory", "target_builder"),
    [
        (_phase_context, PhaseControlApplyPlanBuilder),
        (_lifecycle_context, CompositeLifecycleControlApplyPlanBuilder),
        (_setup_context, SetupControlApplyPlanBuilder),
        (_participant_context, ParticipantControlApplyPlanBuilder),
        (
            lambda: _participant_context(SessionCommandType.REPLACE_PLAYER),
            ParticipantControlApplyPlanBuilder,
        ),
        (_game_rule_context, GameRuleControlApplyPlanBuilder),
        (_reveal_context, GameRuleControlApplyPlanBuilder),
    ],
)
def test_dispatcher_preserves_real_target_builder_outcome(
    context_factory,
    target_builder,
) -> None:
    context = context_factory()

    expected = target_builder().build(context)
    actual = CompositeGameControlApplyPlanBuilder().build(context)

    assert isinstance(expected, BuildPlanReady)
    assert actual == expected


@pytest.mark.parametrize(
    ("command_type", "target_name"),
    [
        (SessionCommandType.START_GAME, "CompositeLifecycleControlApplyPlanBuilder"),
        (SessionCommandType.PAUSE_GAME, "CompositeLifecycleControlApplyPlanBuilder"),
        (SessionCommandType.END_GAME, "CompositeLifecycleControlApplyPlanBuilder"),
        (SessionCommandType.CHANGE_PHASE, "PhaseControlApplyPlanBuilder"),
        (SessionCommandType.SET_SCRIPT, "SetupControlApplyPlanBuilder"),
        (
            SessionCommandType.ASSIGN_CHARACTER,
            "ParticipantControlApplyPlanBuilder",
        ),
        (
            SessionCommandType.REPLACE_PLAYER,
            "ParticipantControlApplyPlanBuilder",
        ),
        (
            SessionCommandType.ACTIVATE_RULE_SET,
            "GameRuleControlApplyPlanBuilder",
        ),
        (
            SessionCommandType.REVEAL_CLUE,
            "GameRuleControlApplyPlanBuilder",
        ),
    ],
)
def test_dispatcher_calls_exactly_one_target_and_returns_its_object(
    monkeypatch: pytest.MonkeyPatch,
    command_type: SessionCommandType,
    target_name: str,
) -> None:
    context = _lifecycle_context()
    object.__setattr__(context.command_intent, "command_type", command_type)
    expected = BuildNonCommit(
        reason=BuildNonCommitReason.EVIDENCE_MISMATCH,
        detail_code="TARGET_OUTCOME",
    )
    calls: list[object] = []

    class TargetBuilder:
        def build(self, received: object) -> BuildNonCommit:
            calls.append(received)
            return expected

    class UnexpectedBuilder:
        def build(self, received: object) -> BuildNonCommit:
            raise AssertionError("dispatcher routed to more than one builder")

    monkeypatch.setattr(dispatcher_module, target_name, TargetBuilder)
    for other_name in {
        "CompositeLifecycleControlApplyPlanBuilder",
        "PhaseControlApplyPlanBuilder",
        "SetupControlApplyPlanBuilder",
        "ParticipantControlApplyPlanBuilder",
        "GameRuleControlApplyPlanBuilder",
    } - {target_name}:
        monkeypatch.setattr(dispatcher_module, other_name, UnexpectedBuilder)

    outcome = CompositeGameControlApplyPlanBuilder().build(context)

    assert outcome is expected
    assert calls == [context]


@pytest.mark.parametrize(
    "command_type",
    [
        SessionCommandType.CREATE_SESSION,
        SessionCommandType.RESUME_GAME,
    ],
)
def test_unimplemented_commands_are_closed_without_target_builder_calls(
    monkeypatch: pytest.MonkeyPatch,
    command_type: SessionCommandType,
) -> None:
    context = _lifecycle_context()
    object.__setattr__(context.command_intent, "command_type", command_type)

    class UnexpectedBuilder:
        def build(self, received: object) -> BuildNonCommit:
            raise AssertionError("unimplemented command reached a target builder")

    monkeypatch.setattr(
        dispatcher_module,
        "CompositeLifecycleControlApplyPlanBuilder",
        UnexpectedBuilder,
    )
    monkeypatch.setattr(
        dispatcher_module,
        "PhaseControlApplyPlanBuilder",
        UnexpectedBuilder,
    )
    monkeypatch.setattr(
        dispatcher_module,
        "SetupControlApplyPlanBuilder",
        UnexpectedBuilder,
    )
    monkeypatch.setattr(
        dispatcher_module,
        "ParticipantControlApplyPlanBuilder",
        UnexpectedBuilder,
    )
    monkeypatch.setattr(
        dispatcher_module,
        "GameRuleControlApplyPlanBuilder",
        UnexpectedBuilder,
        raising=False,
    )

    outcome = CompositeGameControlApplyPlanBuilder().build(context)

    assert outcome == BuildNonCommit(
        reason=BuildNonCommitReason.REDUCER_UNAVAILABLE,
        detail_code=f"{command_type.value}_REDUCER_UNAVAILABLE",
    )


def test_invalid_context_fails_closed_without_builder_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnexpectedBuilder:
        def build(self, received: object) -> BuildNonCommit:
            raise AssertionError("invalid context reached a target builder")

    monkeypatch.setattr(
        dispatcher_module,
        "CompositeLifecycleControlApplyPlanBuilder",
        UnexpectedBuilder,
    )
    monkeypatch.setattr(
        dispatcher_module,
        "PhaseControlApplyPlanBuilder",
        UnexpectedBuilder,
    )
    monkeypatch.setattr(
        dispatcher_module,
        "SetupControlApplyPlanBuilder",
        UnexpectedBuilder,
    )
    monkeypatch.setattr(
        dispatcher_module,
        "ParticipantControlApplyPlanBuilder",
        UnexpectedBuilder,
    )
    monkeypatch.setattr(
        dispatcher_module,
        "GameRuleControlApplyPlanBuilder",
        UnexpectedBuilder,
        raising=False,
    )

    outcome = CompositeGameControlApplyPlanBuilder().build(object())  # type: ignore[arg-type]

    assert outcome == BuildNonCommit(
        reason=BuildNonCommitReason.INVALID_CONTEXT,
        detail_code="CONTROL_APPLY_CONTEXT_TYPE_INVALID",
    )


@pytest.mark.parametrize(
    "malformed_context",
    [
        pytest.param(
            lambda: _malformed_context("intent_object"),
            id="command-intent-object",
        ),
        pytest.param(
            lambda: _malformed_context("command-type-object"),
            id="command-type-object",
        ),
        pytest.param(
            lambda: object.__new__(type(_lifecycle_context())),
            id="control-context-missing-slots",
        ),
    ],
)
def test_malformed_dispatcher_input_fails_closed_without_builder_invocation(
    monkeypatch: pytest.MonkeyPatch,
    malformed_context,
) -> None:
    class UnexpectedBuilder:
        def build(self, received: object) -> BuildNonCommit:
            raise AssertionError("malformed context reached a target builder")

    monkeypatch.setattr(
        dispatcher_module,
        "CompositeLifecycleControlApplyPlanBuilder",
        UnexpectedBuilder,
    )
    monkeypatch.setattr(
        dispatcher_module,
        "PhaseControlApplyPlanBuilder",
        UnexpectedBuilder,
    )
    monkeypatch.setattr(
        dispatcher_module,
        "SetupControlApplyPlanBuilder",
        UnexpectedBuilder,
    )
    monkeypatch.setattr(
        dispatcher_module,
        "ParticipantControlApplyPlanBuilder",
        UnexpectedBuilder,
    )

    outcome = CompositeGameControlApplyPlanBuilder().build(malformed_context())  # type: ignore[arg-type]

    assert isinstance(outcome, BuildNonCommit)
    assert outcome.reason is BuildNonCommitReason.INVALID_CONTEXT


def _malformed_context(kind: str):
    context = _lifecycle_context()
    if kind == "intent_object":
        object.__setattr__(context, "command_intent", object())
    elif kind == "command-type-object":
        object.__setattr__(context.command_intent, "command_type", object())
    else:
        raise AssertionError(f"unknown malformed context kind: {kind}")
    return context


def _static_dependencies(source: str) -> tuple[set[str], set[str], list[str]]:
    tree = ast.parse(source)
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    import_targets = {
        f"{node.module}.{alias.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
        for alias in node.names
    }
    calls = [
        _call_name(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    ]
    return imports, import_targets, calls


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_call_name(node.value)}.{node.attr}"
    return "<dynamic>"


def _forbidden_static_operations(
    *,
    imports: set[str],
    import_targets: set[str],
    calls: list[str],
) -> set[str]:
    forbidden_import_terms = {
        "actor",
        "coordinator",
        "persistence",
        "event_store",
        "eventstore",
        "recovery",
        "notification",
        "plugin",
        "tool",
        "llm",
        "asyncio",
        "registry",
        "importlib",
    }
    forbidden_constructors = {
        "GameEvent",
        "CandidateSessionSnapshot",
        "CandidateGameSnapshot",
        "ControlApplyPlan",
        "ControlRejectPlan",
    }
    forbidden_call_names = {
        "commit",
        "apply",
        "append",
        "notify",
        "recover",
        "import_module",
        "reload",
        "__import__",
    }
    findings = {
        reference
        for reference in imports | import_targets
        if any(term in reference.casefold() for term in forbidden_import_terms)
    }
    for call in calls:
        terminal = call.rsplit(".", maxsplit=1)[-1]
        if (
            terminal in forbidden_constructors
            or "Candidate" in terminal
            or "Snapshot" in terminal
            or terminal.startswith("transition_")
            or terminal.endswith("Coordinator")
            or terminal in forbidden_call_names
        ):
            findings.add(call)
    return findings


def test_static_dependency_guard_detects_direct_and_attribute_violations() -> None:
    source = """
import asyncio
import importlib
from game_runtime.event import GameEvent
from game_runtime.session_control.coordinator import ApplyCoordinator
from game_runtime.session_control.registry import get_builder

def forbidden():
    GameEvent()
    events.GameEvent()
    snapshots.CandidateGameSnapshot()
    snapshots.PhaseSnapshotSlice()
    plans.ControlApplyPlan()
    transition_lifecycle()
    transitions.transition_phase()
    ApplyCoordinator()
    coordinator.commit()
    port.apply()
    log.append()
    notifier.notify()
    recovery.recover()
    importlib.import_module('untrusted')
    __import__('plugin.registry')
    builtins.__import__('plugin.registry')
"""
    imports, import_targets, calls = _static_dependencies(source)

    findings = _forbidden_static_operations(
        imports=imports,
        import_targets=import_targets,
        calls=calls,
    )

    assert findings >= {
        "asyncio",
        "importlib",
        "game_runtime.session_control.coordinator",
        "game_runtime.session_control.registry",
        "GameEvent",
        "events.GameEvent",
        "snapshots.CandidateGameSnapshot",
        "snapshots.PhaseSnapshotSlice",
        "plans.ControlApplyPlan",
        "transition_lifecycle",
        "transitions.transition_phase",
        "ApplyCoordinator",
        "coordinator.commit",
        "port.apply",
        "log.append",
        "notifier.notify",
        "recovery.recover",
        "importlib.import_module",
        "__import__",
        "builtins.__import__",
    }


def test_dispatcher_is_the_only_sync_stateless_control_plane_and_has_no_forbidden_dependencies() -> None:
    tree = ast.parse(inspect.getsource(dispatcher_module))
    classes = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    imports, import_targets, calls = _static_dependencies(
        inspect.getsource(dispatcher_module)
    )
    allowed_imports = {
        "__future__",
        "game_runtime.session_control.apply_plan_builder",
        "game_runtime.session_control.build_context",
        "game_runtime.session_control.commands",
        "game_runtime.session_control.composite_lifecycle_builder",
        "game_runtime.session_control.phase_control_builder",
        "game_runtime.session_control.participant_control_builder",
        "game_runtime.session_control.setup_control_builder",
        "game_runtime.session_control.game_rule_control_builder",
    }

    assert classes == ["CompositeGameControlApplyPlanBuilder"]
    assert CompositeGameControlApplyPlanBuilder.__slots__ == ()
    assert not hasattr(CompositeGameControlApplyPlanBuilder(), "__dict__")
    assert not inspect.iscoroutinefunction(CompositeGameControlApplyPlanBuilder.build)
    assert imports == allowed_imports
    assert not _forbidden_static_operations(
        imports=imports,
        import_targets=import_targets,
        calls=calls,
    )
