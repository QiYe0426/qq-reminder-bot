from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from plugins.agent_tools.policy import (
    ResolvedAgentToolPolicy,
    resolve_agent_tool_policy,
    resolve_all_registered_tool_policies,
)
from plugins.agent_tools.registry import AgentTool, AgentToolMetadata


async def _handler(arguments, context):
    return {"ok": True}


def _tool(**overrides: object) -> AgentTool:
    values: dict[str, object] = {
        "name": "test_tool",
        "definition": {},
        "handler": _handler,
        "metadata": AgentToolMetadata(),
    }
    values.update(overrides)
    return AgentTool(**values)


def _conflict(policy: ResolvedAgentToolPolicy, field: str):
    return next(item for item in policy.conflicts if item.field == field)


def test_metadata_and_legacy_agree() -> None:
    policy = resolve_agent_tool_policy(_tool())

    assert policy.risk_level == "low"
    assert policy.side_effect == "none"
    assert policy.requires_confirmation is False
    assert policy.idempotency_enabled is False
    assert policy.conflicts == ()


def test_risk_downgrade_keeps_legacy_protection() -> None:
    policy = resolve_agent_tool_policy(
        _tool(risk_level="high", metadata=AgentToolMetadata(risk_level="medium"))
    )

    assert policy.risk_level == "high"
    assert _conflict(policy, "risk_level").severity == "security"


def test_critical_risk_projects_to_gateway_compatible_high() -> None:
    policy = resolve_agent_tool_policy(_tool(metadata=AgentToolMetadata(risk_level="critical")))

    assert policy.declared_risk_level == "critical"
    assert policy.risk_level == "high"
    assert _conflict(policy, "risk_level").resolution == "high"


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("none", "none"),
        ("read", "none"),
        ("database_write", "write"),
        ("file_write", "write"),
        ("external_write", "external"),
        ("message_send", "external"),
        ("mixed", "external"),
    ],
)
def test_side_effect_projection(declared: str, expected: str) -> None:
    policy = resolve_agent_tool_policy(_tool(metadata=AgentToolMetadata(side_effect=declared)))

    assert policy.side_effect == expected


def test_side_effect_projection_never_weakens_legacy() -> None:
    policy = resolve_agent_tool_policy(
        _tool(side_effect="external", metadata=AgentToolMetadata(side_effect="database_write"))
    )

    assert policy.side_effect == "external"
    assert _conflict(policy, "side_effect").severity == "security"


def test_required_confirmation_enables_confirmation() -> None:
    policy = resolve_agent_tool_policy(
        _tool(metadata=AgentToolMetadata(confirmation_policy="required"))
    )

    assert policy.requires_confirmation is True
    assert policy.confirmation_source == "metadata"


def test_never_confirmation_does_not_cancel_legacy_requirement() -> None:
    policy = resolve_agent_tool_policy(_tool(requires_confirmation=True))

    assert policy.requires_confirmation is True
    assert policy.confirmation_source == "conservative_merge"
    assert _conflict(policy, "confirmation_policy").severity == "security"


@pytest.mark.parametrize("declared", ["optional", "conditional"])
def test_incomplete_confirmation_policies_fall_back_to_legacy(declared: str) -> None:
    policy = resolve_agent_tool_policy(
        _tool(
            requires_confirmation=True,
            metadata=AgentToolMetadata(confirmation_policy=declared),
        )
    )

    assert policy.requires_confirmation is True
    assert policy.confirmation_source == "legacy"


def test_result_cache_enables_idempotency() -> None:
    policy = resolve_agent_tool_policy(
        _tool(metadata=AgentToolMetadata(idempotency_policy="result_cache"))
    )

    assert policy.idempotency_enabled is True
    assert policy.idempotency_source == "metadata"


def test_none_idempotency_does_not_disable_legacy_protection() -> None:
    policy = resolve_agent_tool_policy(_tool(idempotency_enabled=True))

    assert policy.idempotency_enabled is True
    assert policy.idempotency_source == "conservative_merge"
    assert _conflict(policy, "idempotency_policy").severity == "security"


@pytest.mark.parametrize("legacy", [False, True])
def test_single_flight_falls_back_to_legacy(legacy: bool) -> None:
    policy = resolve_agent_tool_policy(
        _tool(
            idempotency_enabled=legacy,
            metadata=AgentToolMetadata(idempotency_policy="single_flight"),
        )
    )

    assert policy.idempotency_enabled is legacy
    assert policy.idempotency_source == "legacy"


def test_timeout_is_enforced_without_changing_confirmation_timeout() -> None:
    policy = resolve_agent_tool_policy(
        _tool(
            confirmation_timeout=321,
            metadata=AgentToolMetadata(timeout_seconds=15),
        )
    )

    assert policy.timeout_seconds == 15
    assert policy.timeout_enforced is True
    assert policy.confirmation_timeout == 321


def test_output_budget_is_declarative_only() -> None:
    policy = resolve_agent_tool_policy(_tool(metadata=AgentToolMetadata(output_budget=7000)))

    assert policy.output_budget == 7000
    assert policy.output_budget_enforced is False


def test_resource_scope_does_not_change_authorization_projection() -> None:
    policy = resolve_agent_tool_policy(
        _tool(
            requires_admin=False,
            requires_group=False,
            group_scope="none",
            metadata=AgentToolMetadata(resource_scope="target_group"),
        )
    )

    assert policy.resource_scope == "target_group"
    assert policy.authorization_source == "legacy"
    assert policy.requires_admin is False
    assert policy.requires_group is False
    assert policy.group_scope == "none"


def test_resolver_does_not_modify_input() -> None:
    tool = _tool(
        risk_level="high",
        idempotency_temporary_errors=frozenset({"temporary"}),
        metadata=AgentToolMetadata(risk_level="medium"),
    )
    before = tool

    resolve_agent_tool_policy(tool)

    assert tool == before
    assert tool.metadata.risk_level == "medium"
    assert tool.idempotency_temporary_errors == frozenset({"temporary"})


def test_resolved_policy_is_immutable() -> None:
    policy = resolve_agent_tool_policy(_tool())

    with pytest.raises(FrozenInstanceError):
        policy.risk_level = "high"  # type: ignore[misc]
    assert isinstance(policy.conflicts, tuple)
    assert isinstance(policy.idempotency_temporary_errors, frozenset)


def test_resolver_is_deterministic() -> None:
    tool = _tool(
        risk_level="high",
        side_effect="external",
        requires_confirmation=True,
        idempotency_enabled=True,
        metadata=AgentToolMetadata(risk_level="medium", side_effect="database_write"),
    )

    assert resolve_agent_tool_policy(tool) == resolve_agent_tool_policy(tool)


def test_all_registered_policy_snapshot_is_sorted_and_immutable() -> None:
    snapshot = resolve_all_registered_tool_policies()

    assert isinstance(snapshot, tuple)
    assert snapshot
    assert [name for name, _ in snapshot] == sorted(name for name, _ in snapshot)
    assert all(isinstance(policy, ResolvedAgentToolPolicy) for _, policy in snapshot)
