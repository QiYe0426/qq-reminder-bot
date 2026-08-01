from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest

from game_runtime.session_control import (
    ActivateRuleSetPayload,
    ControlGameRuleApplyEvidence,
    ControlRuleSetActivationEvidence,
    GameRuleEvidenceStatus,
    SessionCommandType,
)
from game_runtime.session_control.game_rule_evidence import (
    ControlGameRuleEvidenceError,
)


SCOPE = {
    "game_id": "game-1",
    "session_id": "session-1",
    "observed_state_version": 4,
}


def _activation(**overrides: object) -> ControlRuleSetActivationEvidence:
    values: dict[str, object] = {
        "evidence_schema_version": 1,
        **SCOPE,
        "setup_manifest_reference": "manifest-1",
        "setup_version": 2,
        "committed_rule_set_reference": "rule-set:commit-1",
        "rule_set_version": 3,
        "opaque_hidden_state_reference": "hidden-state:commit-1",
        "hidden_state_version": 5,
        "provenance_reference": "provenance-1",
        "validation_status": GameRuleEvidenceStatus.VERIFIED,
    }
    values.update(overrides)
    return ControlRuleSetActivationEvidence(**values)  # type: ignore[arg-type]


def _payload(**overrides: object) -> ActivateRuleSetPayload:
    values: dict[str, object] = {
        "manifest_reference": "manifest-1",
        "expected_setup_version": 2,
        "expected_game_rule_version": 3,
        "expected_hidden_state_version": 5,
    }
    values.update(overrides)
    return ActivateRuleSetPayload(**values)  # type: ignore[arg-type]


def test_game_rule_evidence_contracts_are_closed_frozen_slotted_values() -> None:
    activation = _activation()
    evidence = ControlGameRuleApplyEvidence(rule_set_activation=activation)

    assert tuple(field.name for field in fields(ControlRuleSetActivationEvidence)) == (
        "evidence_schema_version",
        "game_id",
        "session_id",
        "observed_state_version",
        "setup_manifest_reference",
        "setup_version",
        "committed_rule_set_reference",
        "rule_set_version",
        "opaque_hidden_state_reference",
        "hidden_state_version",
        "provenance_reference",
        "validation_status",
    )
    assert tuple(field.name for field in fields(ControlGameRuleApplyEvidence)) == (
        "rule_set_activation",
    )
    for value in (activation, evidence):
        assert not hasattr(value, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(value, fields(value)[0].name, "changed")

    with pytest.raises(ControlGameRuleEvidenceError):
        ControlGameRuleApplyEvidence(rule_set_activation="not-evidence")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("evidence_schema_version", 2),
        ("evidence_schema_version", True),
        ("game_id", " "),
        ("session_id", "\t"),
        ("setup_manifest_reference", ""),
        ("committed_rule_set_reference", " "),
        ("opaque_hidden_state_reference", ""),
        ("provenance_reference", "\n"),
        ("observed_state_version", -1),
        ("setup_version", True),
        ("rule_set_version", -1),
        ("hidden_state_version", False),
        ("validation_status", "VERIFIED"),
    ],
)
def test_activation_evidence_rejects_invalid_values(
    field_name: str, value: object
) -> None:
    with pytest.raises((ControlGameRuleEvidenceError, TypeError)):
        _activation(**{field_name: value})


def test_evidence_binds_scope_and_observed_global_version() -> None:
    evidence = ControlGameRuleApplyEvidence(rule_set_activation=_activation())

    evidence.validate_bindings(**SCOPE)
    for mismatch in (
        {"game_id": "other-game"},
        {"session_id": "other-session"},
        {"observed_state_version": 5},
    ):
        with pytest.raises(ControlGameRuleEvidenceError):
            evidence.validate_bindings(**(SCOPE | mismatch))


def test_command_validation_requires_exact_verified_activation_evidence() -> None:
    verified = ControlGameRuleApplyEvidence(rule_set_activation=_activation())

    verified.validate_for_command(SessionCommandType.ACTIVATE_RULE_SET, **SCOPE)
    for evidence in (
        ControlGameRuleApplyEvidence(),
        ControlGameRuleApplyEvidence(
            rule_set_activation=_activation(
                validation_status=GameRuleEvidenceStatus.UNKNOWN
            )
        ),
    ):
        with pytest.raises(ControlGameRuleEvidenceError):
            evidence.validate_for_command(
                SessionCommandType.ACTIVATE_RULE_SET, **SCOPE
            )

    for command in set(SessionCommandType) - {SessionCommandType.ACTIVATE_RULE_SET}:
        ControlGameRuleApplyEvidence().validate_for_command(command, **SCOPE)
        with pytest.raises(ControlGameRuleEvidenceError):
            verified.validate_for_command(command, **SCOPE)


@pytest.mark.parametrize(
    "payload",
    [
        _payload(manifest_reference="manifest-other"),
        _payload(expected_setup_version=3),
        _payload(expected_game_rule_version=4),
        _payload(expected_hidden_state_version=6),
    ],
)
def test_activation_evidence_binds_every_payload_version(
    payload: ActivateRuleSetPayload,
) -> None:
    evidence = ControlGameRuleApplyEvidence(rule_set_activation=_activation())

    evidence.validate_payload_binding(SessionCommandType.ACTIVATE_RULE_SET, _payload())
    with pytest.raises(ControlGameRuleEvidenceError):
        evidence.validate_payload_binding(SessionCommandType.ACTIVATE_RULE_SET, payload)
    with pytest.raises(ControlGameRuleEvidenceError):
        evidence.validate_payload_binding(SessionCommandType.PAUSE_GAME, _payload())


def test_game_rule_evidence_contract_has_no_runtime_or_mutable_dependencies() -> None:
    source = Path("game_runtime/session_control/game_rule_evidence.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    forbidden = (
        "actor",
        "session",
        "snapshot",
        "clock",
        "uuid",
        "random",
        "repository",
        "persistence",
        "io",
    )
    assert not any(token in module.lower() for module in imported_modules for token in forbidden)
    assert not any(
        isinstance(node.value, (ast.Dict, ast.List, ast.Set))
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
    )
