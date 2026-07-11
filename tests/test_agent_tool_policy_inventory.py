from __future__ import annotations

from dataclasses import asdict

import plugins.agent_tools  # noqa: F401 - importing the package registers every AgentTool

from plugins.agent_tools.policy import resolve_all_registered_tool_policies
from plugins.agent_tools.registry import get_agent_tool, list_agent_tools


EXPECTED_REGISTERED_TOOL_NAMES = {
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


# Every resolver conflict is explicit. A new, removed, or changed conflict must be
# reviewed instead of being silently accepted by a broad assertion.
EXPECTED_CONFLICT_INVENTORY = {
    ("build_semantic_graph", "risk_level"): {
        "metadata_value": "medium",
        "legacy_value": "high",
        "resolution": "high",
        "severity": "security",
        "why": "The Phase 1 declaration understates the existing high-risk Runtime classification.",
        "safe_now": "Yes. The resolver retains legacy high, so current protection is not reduced.",
        "future_migration": "blocked_until_metadata_is_high_or_stricter",
    },
    ("build_semantic_graph", "idempotency_policy"): {
        "metadata_value": "single_flight",
        "legacy_value": True,
        "resolution": "legacy_enabled",
        "severity": "warning",
        "why": "Legacy idempotency includes result caching and replay, not only single-flight exclusion.",
        "safe_now": "Yes. The resolver preserves the complete legacy idempotency layer.",
        "future_migration": "blocked_until_single_flight_runtime_semantics_exist",
    },
    ("cancel_reminder", "confirmation_policy"): {
        "metadata_value": "optional",
        "legacy_value": False,
        "resolution": "legacy_not_required",
        "severity": "warning",
        "why": "Optional confirmation has no Runtime condition or trigger semantics.",
        "safe_now": "Yes. The resolver leaves the existing legacy behavior unchanged.",
        "future_migration": "blocked_until_optional_runtime_semantics_exist",
    },
    ("create_reminder", "confirmation_policy"): {
        "metadata_value": "optional",
        "legacy_value": False,
        "resolution": "legacy_not_required",
        "severity": "warning",
        "why": "Optional confirmation has no Runtime condition or trigger semantics.",
        "safe_now": "Yes. The resolver leaves the existing legacy behavior unchanged.",
        "future_migration": "blocked_until_optional_runtime_semantics_exist",
    },
    ("generate_daily_report", "risk_level"): {
        "metadata_value": "medium",
        "legacy_value": "high",
        "resolution": "high",
        "severity": "security",
        "why": "The Phase 1 declaration understates the existing high-risk Runtime classification.",
        "safe_now": "Yes. The resolver retains legacy high and its fail-closed protection.",
        "future_migration": "blocked_until_metadata_is_high_or_stricter",
    },
    ("set_group_features", "side_effect"): {
        "metadata_value": "external_write",
        "legacy_value": "write",
        "resolution": "external",
        "severity": "info",
        "why": "Metadata describes a stricter external side effect than the legacy write category.",
        "safe_now": "Yes. The resolver selects the more conservative external classification.",
        "future_migration": "allowed_after_audit_consumer_accepts_resolved_side_effect",
    },
}


# resource_scope is only a declaration. These entries document whether its shape
# resembles legacy authorization, but none authorizes automatic migration.
EXPECTED_RESOURCE_SCOPE_REVIEWS = {
    "build_semantic_graph": "target_group_legacy_protected",
    "cancel_reminder": "user_scope_enforced_outside_generic_authorization",
    "create_reminder": "user_scope_enforced_outside_generic_authorization",
    "generate_daily_report": "target_group_legacy_protected",
    "get_group_context": "current_group_uses_requires_group_not_group_scope",
    "get_group_profile": "target_group_legacy_protected",
    "get_group_status": "target_group_legacy_protected",
    "get_member_profile": "target_group_legacy_protected",
    "get_semantic_graph": "target_group_legacy_protected",
    "list_reminders": "user_scope_enforced_outside_generic_authorization",
    "render_semantic_graph": "target_group_legacy_protected",
    "set_chime": "current_group_not_expressed_by_legacy_group_scope",
    "set_group_features": "target_group_legacy_protected",
}


def _resolved_by_name():
    return dict(resolve_all_registered_tool_policies())


def _actual_conflicts():
    inventory = {}
    for tool_name, policy in resolve_all_registered_tool_policies():
        for conflict in policy.conflicts:
            inventory[(tool_name, conflict.field)] = {
                "metadata_value": conflict.metadata_value,
                "legacy_value": conflict.legacy_value,
                "resolution": conflict.resolution,
                "severity": conflict.severity,
            }
    return inventory


def _resource_scope_review(tool_name: str) -> str | None:
    tool = get_agent_tool(tool_name)
    assert tool is not None
    scope = tool.metadata.resource_scope
    if scope == "none":
        return None
    if scope == "target_group":
        if tool.group_scope == "private_explicit" and tool.requires_target_group_admin:
            return "target_group_legacy_protected"
        return "target_group_not_fully_expressed_by_legacy_authorization"
    if scope == "current_group":
        if tool.requires_group:
            return "current_group_uses_requires_group_not_group_scope"
        return "current_group_not_expressed_by_legacy_group_scope"
    if scope == "user":
        return "user_scope_enforced_outside_generic_authorization"
    return f"{scope}_has_no_legacy_authorization_equivalent"


def test_registered_tool_inventory_is_explicit() -> None:
    assert {tool.name for tool in list_agent_tools()} == EXPECTED_REGISTERED_TOOL_NAMES
    assert set(_resolved_by_name()) == EXPECTED_REGISTERED_TOOL_NAMES


def test_known_conflict_inventory_is_complete_and_exact() -> None:
    expected_runtime_fields = {
        key: {
            "metadata_value": value["metadata_value"],
            "legacy_value": value["legacy_value"],
            "resolution": value["resolution"],
            "severity": value["severity"],
        }
        for key, value in EXPECTED_CONFLICT_INVENTORY.items()
    }

    assert _actual_conflicts() == expected_runtime_fields
    assert all(item["why"] for item in EXPECTED_CONFLICT_INVENTORY.values())
    assert all(item["safe_now"].startswith("Yes.") for item in EXPECTED_CONFLICT_INVENTORY.values())
    assert all(item["future_migration"] for item in EXPECTED_CONFLICT_INVENTORY.values())


def test_resource_scope_authorization_reviews_are_explicit() -> None:
    actual = {
        tool.name: review
        for tool in list_agent_tools()
        if (review := _resource_scope_review(tool.name)) is not None
    }

    assert actual == EXPECTED_RESOURCE_SCOPE_REVIEWS


def test_all_registered_tools_policy_security_invariants() -> None:
    resolved_by_name = _resolved_by_name()
    risk_order = {"low": 0, "medium": 1, "high": 2}
    side_effect_order = {"none": 0, "write": 1, "external": 2}

    for tool in list_agent_tools():
        resolved = resolved_by_name[tool.name]

        if tool.risk_level == "high":
            assert resolved.risk_level == "high", tool.name
        assert risk_order[resolved.risk_level] >= risk_order[tool.risk_level], tool.name
        assert side_effect_order[resolved.side_effect] >= side_effect_order[tool.side_effect], tool.name

        if tool.requires_confirmation:
            assert resolved.requires_confirmation is True, tool.name
        if tool.idempotency_enabled:
            assert resolved.idempotency_enabled is True, tool.name
        if tool.requires_target_group_admin:
            assert resolved.requires_target_group_admin is True, tool.name

        assert resolved.requires_feature == tool.requires_feature, tool.name
        assert resolved.requires_admin == tool.requires_admin, tool.name
        assert resolved.requires_group == tool.requires_group, tool.name
        assert resolved.group_scope == tool.group_scope, tool.name
        assert resolved.requires_target_group_admin == tool.requires_target_group_admin, tool.name
        assert resolved.authorization_source == "legacy", tool.name


def test_inventory_contains_complete_declaration_legacy_and_resolution_views() -> None:
    for tool_name, resolved in resolve_all_registered_tool_policies():
        tool = get_agent_tool(tool_name)
        assert tool is not None

        declaration = asdict(tool.metadata)
        legacy = {
            "risk_level": tool.risk_level,
            "side_effect": tool.side_effect,
            "requires_confirmation": tool.requires_confirmation,
            "idempotency_enabled": tool.idempotency_enabled,
            "requires_feature": tool.requires_feature,
            "requires_admin": tool.requires_admin,
            "requires_group": tool.requires_group,
            "group_scope": tool.group_scope,
            "requires_target_group_admin": tool.requires_target_group_admin,
        }
        resolution = {
            "risk_level": resolved.risk_level,
            "side_effect": resolved.side_effect,
            "requires_confirmation": resolved.requires_confirmation,
            "idempotency_enabled": resolved.idempotency_enabled,
            "requires_feature": resolved.requires_feature,
            "requires_admin": resolved.requires_admin,
            "requires_group": resolved.requires_group,
            "group_scope": resolved.group_scope,
            "requires_target_group_admin": resolved.requires_target_group_admin,
        }

        assert declaration == {
            "risk_level": resolved.declared_risk_level,
            "side_effect": resolved.declared_side_effect,
            "resource_scope": resolved.resource_scope,
            "confirmation_policy": resolved.confirmation_policy,
            "idempotency_policy": resolved.idempotency_policy,
            "timeout_seconds": resolved.timeout_seconds,
            "output_budget": resolved.output_budget,
        }
        assert legacy["requires_feature"] == resolved.requires_feature
        assert legacy["requires_admin"] == resolved.requires_admin
        assert legacy["requires_group"] == resolved.requires_group
        assert legacy["group_scope"] == resolved.group_scope
        assert legacy["requires_target_group_admin"] == resolved.requires_target_group_admin
        assert resolution == {
            "risk_level": resolved.risk_level,
            "side_effect": resolved.side_effect,
            "requires_confirmation": resolved.requires_confirmation,
            "idempotency_enabled": resolved.idempotency_enabled,
            "requires_feature": resolved.requires_feature,
            "requires_admin": resolved.requires_admin,
            "requires_group": resolved.requires_group,
            "group_scope": resolved.group_scope,
            "requires_target_group_admin": resolved.requires_target_group_admin,
        }
