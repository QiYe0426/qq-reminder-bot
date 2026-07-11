from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from plugins import agent_tool_access
from plugins.agent_tools.authorization_policy import AuthorizationMetadata, resolve_authorization_policy_with_metadata
from plugins.agent_tools.authorization_shadow import (
    OwnershipPolicyResult,
    compare_authorization_decisions,
    evaluate_metadata_authorization,
)
from plugins.agent_tools.registry import AgentTool, AgentToolMetadata


async def _handler(arguments, context):
    return {"ok": True}


def _tool(name: str, **legacy: object) -> AgentTool:
    return AgentTool(
        name=name,
        definition={},
        handler=_handler,
        metadata=AgentToolMetadata(resource_scope="target_group"),
        **legacy,
    )


def _policy(tool: AgentTool, **metadata: object):
    defaults: dict[str, object] = {"target_scope": "target_group"}
    defaults.update(metadata)
    return resolve_authorization_policy_with_metadata(tool, AuthorizationMetadata(**defaults))


def _context(target_type: str, target_id: str, **updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "_target_type": target_type,
        "_target_id": target_id,
        "_user_id": "10001",
        "_is_admin": False,
    }
    value.update(updates)
    return value


@pytest.fixture(autouse=True)
def allow_unrelated_group_controls(monkeypatch) -> None:
    async def feature_enabled(group_id, feature):
        return True

    async def tool_enabled(group_id, tool_name):
        return True

    monkeypatch.setattr(agent_tool_access, "is_group_feature_enabled", feature_enabled)
    monkeypatch.setattr(agent_tool_access, "agent_tool_enabled_for_group", tool_enabled)


def test_current_group_is_bound_to_server_session(monkeypatch) -> None:
    tool = _tool("engine_current_group", group_scope="current")
    policy = _policy(tool)

    result = asyncio.run(
        evaluate_metadata_authorization(
            tool_name=tool.name,
            arguments={},
            context=_context("group", "20001"),
            policy=policy,
        )
    )

    assert result.allowed is True
    assert result.effective_group_id == "20001"


def test_private_explicit_group_is_resolved_from_validated_argument() -> None:
    tool = _tool("engine_private_explicit", group_scope="private_explicit")
    policy = _policy(tool)

    result = asyncio.run(
        evaluate_metadata_authorization(
            tool_name=tool.name,
            arguments={"group_id": "20002"},
            context=_context("private", "10001"),
            policy=policy,
        )
    )

    assert result.allowed is True
    assert result.effective_group_id == "20002"


def test_model_supplied_cross_group_id_is_rejected() -> None:
    tool = _tool("engine_cross_group", group_scope="current")
    policy = _policy(tool)

    result = asyncio.run(
        evaluate_metadata_authorization(
            tool_name=tool.name,
            arguments={"group_id": "99999"},
            context=_context("group", "20001"),
            policy=policy,
        )
    )

    assert result.allowed is False
    assert result.reason_codes == ("group_permission_denied",)
    assert result.effective_group_id == ""


def test_target_group_admin_uses_server_verification(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    async def verify(user_id, group_id, context):
        calls.append((user_id, group_id))
        return True

    monkeypatch.setattr(agent_tool_access, "target_group_admin_authorized", verify)
    tool = _tool("engine_target_admin", group_scope="private_explicit")
    policy = _policy(
        tool,
        admin_requirements=frozenset({"target_group_admin"}),
    )

    result = asyncio.run(
        evaluate_metadata_authorization(
            tool_name=tool.name,
            arguments={"group_id": "20002"},
            context=_context("private", "10001"),
            policy=policy,
        )
    )

    assert result.allowed is True
    assert calls == [("10001", "20002")]


def test_feature_disabled_denies_for_effective_group(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    async def feature_enabled(group_id, feature):
        calls.append((str(group_id), feature))
        return False

    monkeypatch.setattr(agent_tool_access, "is_group_feature_enabled", feature_enabled)
    tool = _tool("engine_feature", group_scope="current")
    policy = _policy(
        tool,
        feature_requirements=frozenset({"collector"}),
    )

    result = asyncio.run(
        evaluate_metadata_authorization(
            tool_name=tool.name,
            arguments={},
            context=_context("group", "20001"),
            policy=policy,
        )
    )

    assert result.allowed is False
    assert result.reason_codes == ("feature_disabled",)
    assert result.failed_requirements == ("feature:collector",)
    assert calls == [("20001", "collector")]


def test_feature_requirement_without_effective_group_denies() -> None:
    tool = _tool("engine_feature_without_group")
    policy = _policy(tool, feature_requirements=frozenset({"ai_chat"}))
    policy = replace(policy, tool_switch_policy="effective_group_required")

    result = asyncio.run(
        evaluate_metadata_authorization(
            tool_name=tool.name,
            arguments={},
            context=_context("private", "10001"),
            policy=policy,
        )
    )

    assert result.allowed is False
    assert result.reason_codes == ("missing_effective_group",)
    assert result.failed_requirements == ("feature:ai_chat",)


def test_tool_switch_disabled_denies(monkeypatch) -> None:
    async def tool_enabled(group_id, tool_name):
        return False

    monkeypatch.setattr(agent_tool_access, "agent_tool_enabled_for_group", tool_enabled)
    tool = _tool("engine_tool_switch", group_scope="current")
    policy = _policy(tool)

    result = asyncio.run(
        evaluate_metadata_authorization(
            tool_name=tool.name,
            arguments={},
            context=_context("group", "20001"),
            policy=policy,
        )
    )

    assert result.allowed is False
    assert result.reason_codes == ("tool_not_allowed",)


def test_target_admin_api_failure_denies(monkeypatch) -> None:
    async def fail(user_id, group_id, context):
        raise RuntimeError("api unavailable")

    monkeypatch.setattr(agent_tool_access, "target_group_admin_authorized", fail)
    tool = _tool("engine_admin_api_failure", group_scope="private_explicit")
    policy = _policy(
        tool,
        admin_requirements=frozenset({"target_group_admin"}),
    )

    result = asyncio.run(
        evaluate_metadata_authorization(
            tool_name=tool.name,
            arguments={"group_id": "20002"},
            context=_context("private", "10001"),
            policy=policy,
        )
    )

    assert result.allowed is False
    assert result.reason_codes == ("target_group_admin_denied",)


def test_unimplemented_reminder_ownership_denies() -> None:
    tool = _tool("engine_reminder_ownership")
    policy = _policy(tool, ownership_policy="authorized_reminder_target")

    result = asyncio.run(
        evaluate_metadata_authorization(
            tool_name=tool.name,
            arguments={},
            context=_context("group", "20001"),
            policy=policy,
        )
    )

    assert result.allowed is False
    assert result.reason_codes == ("ownership_policy_unresolved",)
    assert result.failed_requirements == ("ownership:authorized_reminder_target",)


def test_ownership_adapter_is_injected_not_hardcoded() -> None:
    requests = []

    async def current_user(request):
        requests.append(request)
        return OwnershipPolicyResult(True)

    tool = _tool("engine_current_user")
    policy = _policy(tool, ownership_policy="current_user")

    result = asyncio.run(
        evaluate_metadata_authorization(
            tool_name=tool.name,
            arguments={"resource_id": "opaque"},
            context=_context("private", "10001"),
            policy=policy,
            ownership_adapters={"current_user": current_user},
        )
    )

    assert result.allowed is True
    assert requests[0].tool_name == tool.name
    assert requests[0].policy_name == "current_user"


@pytest.mark.parametrize("target_type", ["channel", ""])
def test_unknown_session_type_denies(target_type: str) -> None:
    tool = _tool("engine_unknown_session")
    policy = _policy(tool)

    result = asyncio.run(
        evaluate_metadata_authorization(
            tool_name=tool.name,
            arguments={},
            context=_context(target_type, "target"),
            policy=policy,
        )
    )

    assert result.allowed is False
    assert result.reason_codes == ("unsupported_session_type",)


def test_independent_metadata_decision_can_be_shadow_compared() -> None:
    tool = _tool("engine_shadow_compare")
    policy = _policy(tool, session_requirement="group_only")
    metadata_result = asyncio.run(
        evaluate_metadata_authorization(
            tool_name=tool.name,
            arguments={},
            context=_context("private", "10001"),
            policy=policy,
        )
    )

    comparison = compare_authorization_decisions(
        legacy_allowed=True,
        metadata_result=metadata_result,
        policy=policy,
    )

    assert comparison.category == "LEGACY_ALLOW_METADATA_DENY"
