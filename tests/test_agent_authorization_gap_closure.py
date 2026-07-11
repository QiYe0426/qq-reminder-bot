from __future__ import annotations

import asyncio
from collections import Counter
from types import SimpleNamespace

import pytest

import plugins.agent_tools  # noqa: F401 - register all AgentTools
from plugins import agent_tool_access
from plugins.agent_tools.authorization_policy import (
    AuthorizationMetadata,
    resolve_authorization_policy,
    resolve_authorization_policy_with_metadata,
)
from plugins.agent_tools.authorization_shadow import (
    PermissionPolicyResult,
    compare_authorization_decisions,
    evaluate_metadata_authorization,
)
from plugins.agent_tools.registry import AgentTool, AgentToolMetadata, get_agent_tool, list_agent_tools


async def _handler(arguments, context):
    return {"ok": True}


def _tool(name: str) -> AgentTool:
    return AgentTool(
        name=name,
        definition={},
        handler=_handler,
        metadata=AgentToolMetadata(resource_scope="user"),
    )


def _context(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "_target_type": "group",
        "_target_id": "20001",
        "_user_id": "10001",
        "_is_admin": False,
    }
    value.update(updates)
    return value


@pytest.fixture(autouse=True)
def allow_group_controls(monkeypatch) -> None:
    async def enabled(*args):
        return True

    monkeypatch.setattr(agent_tool_access, "is_group_feature_enabled", enabled)
    monkeypatch.setattr(agent_tool_access, "agent_tool_enabled_for_group", enabled)


@pytest.mark.parametrize("tool_name", ["create_reminder", "list_reminders", "cancel_reminder"])
def test_current_user_ownership_closes_registered_reminder_gap(tool_name: str) -> None:
    tool = _tool(tool_name)
    policy = resolve_authorization_policy(tool)
    context = _context()
    if tool_name == "create_reminder":
        context["_scope"] = SimpleNamespace(user_id="10001")

    metadata = asyncio.run(
        evaluate_metadata_authorization(
            tool_name=tool_name,
            arguments={},
            context=context,
            policy=policy,
        )
    )
    comparison = compare_authorization_decisions(
        legacy_allowed=True,
        metadata_result=metadata,
        policy=policy,
    )

    assert metadata.allowed is True
    assert comparison.category == "ALLOW_ALLOW"


def test_model_user_id_spoof_is_not_used_for_ownership() -> None:
    tool = _tool("create_reminder")
    policy = resolve_authorization_policy(tool)

    metadata = asyncio.run(
        evaluate_metadata_authorization(
            tool_name=tool.name,
            arguments={"user_id": "attacker-selected-user"},
            context=_context(_scope=SimpleNamespace(user_id="10001")),
            policy=policy,
        )
    )

    assert metadata.allowed is True


@pytest.mark.parametrize(
    ("context", "reason"),
    [
        ({"_scope": None}, "current_user_owner_unresolved"),
        ({"_scope": SimpleNamespace(user_id="99999")}, "current_user_owner_mismatch"),
        ({"_user_id": ""}, "current_user_missing"),
    ],
)
def test_missing_or_mismatched_ownership_context_denies(
    context: dict[str, object], reason: str
) -> None:
    tool = _tool("create_reminder")
    policy = resolve_authorization_policy(tool)

    metadata = asyncio.run(
        evaluate_metadata_authorization(
            tool_name=tool.name,
            arguments={"user_id": "10001"},
            context=_context(**context),
            policy=policy,
        )
    )

    assert metadata.allowed is False
    assert metadata.reason_codes == (reason,)


def test_permission_adapter_can_explicitly_authorize() -> None:
    calls = []

    async def resource_owner(request):
        calls.append(request)
        return PermissionPolicyResult(True)

    tool = _tool("permission_adapter_test")
    policy = resolve_authorization_policy_with_metadata(
        tool,
        AuthorizationMetadata(required_permissions=frozenset({"resource.owner"})),
    )

    metadata = asyncio.run(
        evaluate_metadata_authorization(
            tool_name=tool.name,
            arguments={},
            context=_context(),
            policy=policy,
            permission_adapters={"resource.owner": resource_owner},
        )
    )

    assert metadata.allowed is True
    assert calls[0].permission == "resource.owner"


def test_unknown_permission_denies() -> None:
    tool = _tool("unknown_permission_test")
    policy = resolve_authorization_policy_with_metadata(
        tool,
        AuthorizationMetadata(required_permissions=frozenset({"unknown.permission"})),
    )

    metadata = asyncio.run(
        evaluate_metadata_authorization(
            tool_name=tool.name,
            arguments={},
            context=_context(),
            policy=policy,
        )
    )

    assert metadata.allowed is False
    assert metadata.reason_codes == ("permission_unresolved",)
    assert metadata.failed_requirements == ("permission:unknown.permission",)


def test_registered_tools_have_no_required_permission_gap() -> None:
    for tool in list_agent_tools():
        policy = resolve_authorization_policy(tool)
        assert policy.required_permissions == frozenset(), tool.name


def test_reminder_shadow_inventory_has_no_unexplained_tightening() -> None:
    categories = Counter()
    for tool_name in ("create_reminder", "list_reminders", "cancel_reminder"):
        tool = _tool(tool_name)
        policy = resolve_authorization_policy(tool)
        context = _context()
        if tool_name == "create_reminder":
            context["_scope"] = SimpleNamespace(user_id="10001")
        metadata = asyncio.run(
            evaluate_metadata_authorization(
                tool_name=tool_name,
                arguments={},
                context=context,
                policy=policy,
            )
        )
        comparison = compare_authorization_decisions(
            legacy_allowed=True,
            metadata_result=metadata,
            policy=policy,
        )
        categories[comparison.category] += 1

    assert categories == Counter({"ALLOW_ALLOW": 3})
    assert categories["LEGACY_ALLOW_METADATA_DENY"] == 0
    assert categories["LEGACY_DENY_METADATA_ALLOW"] == 0


@pytest.mark.parametrize("tool_name", ["create_reminder", "list_reminders", "cancel_reminder"])
def test_private_reminder_feature_and_tool_switch_are_group_conditional(tool_name: str) -> None:
    tool = _tool(tool_name)
    policy = resolve_authorization_policy(tool)
    context = _context(_target_type="private", _target_id="10001")
    if tool_name == "create_reminder":
        context["_scope"] = SimpleNamespace(user_id="10001")

    metadata = asyncio.run(
        evaluate_metadata_authorization(
            tool_name=tool_name,
            arguments={},
            context=context,
            policy=policy,
        )
    )

    assert metadata.allowed is True
    assert metadata.effective_group_id == ""


def test_set_chime_builtin_and_registered_authorization_are_currently_consistent() -> None:
    tool = get_agent_tool("set_chime")
    capability = agent_tool_access.get_agent_tool_capability("set_chime")
    assert tool is not None
    assert capability is not None
    policy = resolve_authorization_policy(tool)

    assert capability.source == "builtin"
    assert capability.requires_feature == tool.requires_feature
    assert capability.requires_admin == tool.requires_admin is True
    assert capability.requires_group == tool.requires_group is False
    assert capability.group_scope == tool.group_scope == "none"
    assert capability.requires_target_group_admin == tool.requires_target_group_admin is False
    assert policy.feature_requirements == frozenset()
    assert policy.admin_requirements == frozenset({"session_admin"})
    assert policy.group_requirement == "none"
