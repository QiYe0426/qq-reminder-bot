from __future__ import annotations

import itertools
from dataclasses import FrozenInstanceError

import pytest

import plugins.agent_tools  # noqa: F401 - register all AgentTools for the shadow inventory
from plugins.agent_tools.authorization_policy import (
    AUTHORIZATION_METADATA,
    UNSET,
    AuthorizationMetadata,
    ResolvedAuthorizationPolicy,
    resolve_all_registered_authorization_policies,
    resolve_authorization_policy,
    resolve_authorization_policy_with_metadata,
)
from plugins.agent_tools.registry import AgentTool, AgentToolMetadata, get_agent_tool, list_agent_tools


EXPECTED_TOOL_NAMES = {
    "build_semantic_graph",
    "cancel_reminder",
    "create_reminder",
    "generate_daily_report",
    "get_group_context",
    "get_group_profile",
    "get_group_status",
    "get_member_profile",
    "get_semantic_graph",
    "list_reminders",
    "render_semantic_graph",
    "search_sts2_knowledge",
    "set_chime",
    "set_group_features",
}


async def _handler(arguments, context):
    return {"ok": True}


def _tool(**updates: object) -> AgentTool:
    values: dict[str, object] = {
        "name": "authorization_shadow_test",
        "definition": {},
        "handler": _handler,
        "metadata": AgentToolMetadata(resource_scope="target_group"),
    }
    values.update(updates)
    return AgentTool(**values)


def _requirements_allow(policy, facts: dict[str, bool]) -> bool:
    if policy.session_requirement == "deny_all" or policy.group_requirement == "deny_all":
        return False
    if policy.session_requirement == "group_only" and not facts["session_group"]:
        return False
    if policy.session_requirement == "private_only" and facts["session_group"]:
        return False
    if "session_admin" in policy.admin_requirements and not facts["session_admin"]:
        return False
    if "target_group_admin" in policy.admin_requirements and not facts["target_admin"]:
        return False
    if policy.feature_requirements and not facts["features_enabled"]:
        return False
    if policy.group_requirement != "none" and not facts["group_requirement_satisfied"]:
        return False
    if policy.tool_switch_policy != "not_required" and not facts["tool_switch_enabled"]:
        return False
    return True


def test_authorization_metadata_uses_unset_not_weak_defaults() -> None:
    declaration = AuthorizationMetadata()

    assert declaration.target_scope is UNSET
    assert declaration.required_permissions is UNSET
    assert declaration.feature_requirements is UNSET
    assert declaration.admin_requirements is UNSET
    assert declaration.session_requirement is UNSET
    assert declaration.group_requirement is UNSET
    assert declaration.tool_switch_policy is UNSET
    assert declaration.ownership_policy is UNSET


def test_shadow_inventory_is_complete_and_conflict_free() -> None:
    inventory = dict(resolve_all_registered_authorization_policies())

    assert set(inventory) == EXPECTED_TOOL_NAMES
    assert set(AUTHORIZATION_METADATA) == EXPECTED_TOOL_NAMES
    assert all(policy.source == "conservative_merge" for policy in inventory.values())
    assert {name: policy.conflicts for name, policy in inventory.items()} == {
        name: () for name in EXPECTED_TOOL_NAMES
    }


def test_inventory_contains_legacy_metadata_and_resolved_views() -> None:
    for name, resolved in resolve_all_registered_authorization_policies():
        tool = get_agent_tool(name)
        assert tool is not None
        declaration = resolved.metadata_declaration
        legacy = resolved.legacy_projection

        assert declaration.target_scope == tool.metadata.resource_scope
        assert legacy.feature_requirements == (
            frozenset({tool.requires_feature}) if tool.requires_feature else frozenset()
        )
        assert ("session_admin" in legacy.admin_requirements) is tool.requires_admin
        assert ("target_group_admin" in legacy.admin_requirements) is tool.requires_target_group_admin
        assert legacy.session_requirement == ("group_only" if tool.requires_group else "any")
        assert legacy.group_requirement == tool.group_scope
        assert legacy.required_permissions.issubset(resolved.required_permissions)
        assert legacy.feature_requirements.issubset(resolved.feature_requirements)
        assert legacy.admin_requirements.issubset(resolved.admin_requirements)


def test_legacy_authorization_requirements_cannot_be_removed() -> None:
    tool = _tool(
        requires_feature="collector",
        requires_admin=True,
        requires_group=True,
        group_scope="current",
        requires_target_group_admin=True,
    )
    relaxed = AuthorizationMetadata(
        target_scope="none",
        required_permissions=frozenset(),
        feature_requirements=frozenset(),
        admin_requirements=frozenset(),
        session_requirement="any",
        group_requirement="none",
        tool_switch_policy="not_required",
    )

    resolved = resolve_authorization_policy_with_metadata(tool, relaxed)

    assert resolved.feature_requirements == frozenset({"collector"})
    assert resolved.admin_requirements == frozenset({"session_admin", "target_group_admin"})
    assert resolved.session_requirement == "group_only"
    assert resolved.group_requirement == "current"
    assert resolved.tool_switch_policy == "effective_group_required"
    assert {conflict.field for conflict in resolved.conflicts} == {
        "feature_requirements",
        "admin_requirements",
        "session_requirement",
        "group_requirement",
        "tool_switch_policy",
    }
    assert all(conflict.severity == "security" for conflict in resolved.conflicts)


def test_permissions_features_and_admins_use_constraint_union() -> None:
    tool = _tool(requires_feature="legacy_feature", requires_admin=True)
    declaration = AuthorizationMetadata(
        required_permissions=frozenset({"resource.owner"}),
        feature_requirements=frozenset({"metadata_feature"}),
        admin_requirements=frozenset({"target_group_admin"}),
    )

    resolved = resolve_authorization_policy_with_metadata(tool, declaration)

    assert resolved.required_permissions == frozenset({"resource.owner"})
    assert resolved.feature_requirements == frozenset({"legacy_feature", "metadata_feature"})
    assert resolved.admin_requirements == frozenset({"session_admin", "target_group_admin"})


@pytest.mark.parametrize(
    ("legacy", "declared", "expected", "has_conflict"),
    [
        ("none", "none", "none", False),
        ("none", "current", "current", False),
        ("current", "none", "current", True),
        ("current", "private_explicit", "current", True),
        ("private_explicit", "current", "current", False),
        ("private_explicit", "none", "private_explicit", True),
        ("none", "private_explicit", "deny_all", True),
    ],
)
def test_group_requirement_uses_compatibility_matrix(
    legacy: str,
    declared: str,
    expected: str,
    has_conflict: bool,
) -> None:
    resolved = resolve_authorization_policy_with_metadata(
        _tool(group_scope=legacy),
        AuthorizationMetadata(group_requirement=declared),
    )

    assert resolved.group_requirement == expected
    assert bool(resolved.conflicts) is has_conflict


def test_resource_scope_does_not_generate_permissions() -> None:
    tool = _tool(name="uncatalogued_resource_scope_only")

    resolved = resolve_authorization_policy(tool)

    assert resolved.target_scope == "target_group"
    assert resolved.required_permissions == frozenset()
    assert resolved.admin_requirements == frozenset()
    assert resolved.session_requirement == "any"
    assert resolved.group_requirement == "none"
    assert resolved.source == "legacy"


def test_resolver_is_immutable_and_deterministic() -> None:
    tool = _tool(requires_admin=True)
    declaration = AuthorizationMetadata(admin_requirements=frozenset())
    first = resolve_authorization_policy_with_metadata(tool, declaration)
    second = resolve_authorization_policy_with_metadata(tool, declaration)

    assert first == second
    assert isinstance(first, ResolvedAuthorizationPolicy)
    assert isinstance(first.conflicts, tuple)
    assert isinstance(first.admin_requirements, frozenset)
    with pytest.raises(FrozenInstanceError):
        first.source = "metadata"  # type: ignore[misc]


def test_all_registered_tools_policy_security_invariants() -> None:
    policies = dict(resolve_all_registered_authorization_policies())

    for tool in list_agent_tools():
        resolved = policies[tool.name]
        legacy = resolved.legacy_projection
        assert legacy.required_permissions.issubset(resolved.required_permissions), tool.name
        assert legacy.feature_requirements.issubset(resolved.feature_requirements), tool.name
        assert legacy.admin_requirements.issubset(resolved.admin_requirements), tool.name
        if tool.requires_group:
            assert resolved.session_requirement in {"group_only", "deny_all"}, tool.name
        if tool.group_scope == "current":
            assert resolved.group_requirement in {"current", "deny_all"}, tool.name
        if tool.group_scope == "private_explicit":
            assert resolved.group_requirement in {"private_explicit", "current", "deny_all"}, tool.name
        if tool.requires_target_group_admin:
            assert "target_group_admin" in resolved.admin_requirements, tool.name


def test_shadow_compare_never_turns_legacy_deny_into_resolved_allow() -> None:
    categories = {
        "allow/allow": 0,
        "deny/deny": 0,
        "legacy deny + resolved allow": 0,
        "legacy allow + resolved deny": 0,
    }
    fact_names = (
        "session_group",
        "session_admin",
        "target_admin",
        "features_enabled",
        "group_requirement_satisfied",
        "tool_switch_enabled",
    )

    for _, resolved in resolve_all_registered_authorization_policies():
        for values in itertools.product((False, True), repeat=len(fact_names)):
            facts = dict(zip(fact_names, values, strict=True))
            legacy_allowed = _requirements_allow(resolved.legacy_projection, facts)
            resolved_allowed = _requirements_allow(resolved, facts)
            if legacy_allowed and resolved_allowed:
                categories["allow/allow"] += 1
            elif not legacy_allowed and not resolved_allowed:
                categories["deny/deny"] += 1
            elif not legacy_allowed and resolved_allowed:
                categories["legacy deny + resolved allow"] += 1
            else:
                categories["legacy allow + resolved deny"] += 1

    assert categories["allow/allow"] > 0
    assert categories["deny/deny"] > 0
    assert categories["legacy deny + resolved allow"] == 0
    assert categories["legacy allow + resolved deny"] == 0
