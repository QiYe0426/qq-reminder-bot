from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from plugins import agent_tool_access
from plugins.agent_tool_access import AgentToolAuthorization
from plugins.agent_tools import gateway
from plugins.agent_tools.authorization_policy import AuthorizationMetadata, resolve_authorization_policy_with_metadata
from plugins.agent_tools.authorization_shadow import AuthorizationDecisionComparison, ShadowAuthorizationResult
from plugins.agent_tools.registry import AgentTool, AgentToolMetadata


def _definition(name: str) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Independent authorization Gateway shadow test.",
            "parameters": {
                "type": "object",
                "properties": {"group_id": {"type": "string"}},
                "additionalProperties": False,
            },
        },
    }


def _tool(name: str, handler, **legacy: object) -> AgentTool:
    return AgentTool(
        name=name,
        definition=_definition(name),
        handler=handler,
        metadata=AgentToolMetadata(resource_scope="target_group"),
        **legacy,
    )


def _context(target_type: str, target_id: str, **updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "_target_type": target_type,
        "_target_id": target_id,
        "_user_id": "10001",
        "_is_admin": True,
        "_tool_call_id": "shadow-engine-call",
        "_invocation_source": "agent",
    }
    value.update(updates)
    return value


@pytest.mark.parametrize(
    (
        "scenario",
        "legacy",
        "arguments",
        "context",
        "legacy_allowed",
        "feature_enabled",
        "tool_enabled",
        "target_admin",
        "expected_category",
        "expected_handler_calls",
    ),
    [
        (
            "ordinary_group",
            {},
            {},
            _context("group", "20001"),
            True,
            True,
            True,
            True,
            "ALLOW_ALLOW",
            1,
        ),
        (
            "private_target_group",
            {"group_scope": "private_explicit"},
            {"group_id": "20002"},
            _context("private", "10001"),
            True,
            True,
            True,
            True,
            "ALLOW_ALLOW",
            1,
        ),
        (
            "cross_group_id",
            {"group_scope": "current"},
            {"group_id": "99999"},
            _context("group", "20001"),
            False,
            True,
            True,
            True,
            "DENY_DENY",
            0,
        ),
        (
            "target_admin",
            {"group_scope": "private_explicit", "requires_target_group_admin": True},
            {"group_id": "20002"},
            _context("private", "10001"),
            True,
            True,
            True,
            True,
            "ALLOW_ALLOW",
            1,
        ),
        (
            "feature_disabled",
            {"group_scope": "current", "requires_feature": "collector"},
            {},
            _context("group", "20001"),
            False,
            False,
            True,
            True,
            "DENY_DENY",
            0,
        ),
        (
            "tool_switch_disabled",
            {"group_scope": "current"},
            {},
            _context("group", "20001"),
            False,
            True,
            False,
            True,
            "DENY_DENY",
            0,
        ),
    ],
)
def test_gateway_runs_independent_metadata_shadow_for_real_scenarios(
    monkeypatch,
    scenario: str,
    legacy: dict[str, object],
    arguments: dict[str, object],
    context: dict[str, object],
    legacy_allowed: bool,
    feature_enabled: bool,
    tool_enabled: bool,
    target_admin: bool,
    expected_category: str,
    expected_handler_calls: int,
) -> None:
    handler_calls = 0
    comparisons: list[AuthorizationDecisionComparison] = []

    async def handler(values, tool_context):
        nonlocal handler_calls
        handler_calls += 1
        return {"ok": True}

    tool = _tool(f"gateway_shadow_{scenario}", handler, **legacy)
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)

    async def authorize(tool_name, values, tool_context):
        return AgentToolAuthorization(
            legacy_allowed,
            error="tool_not_allowed" if not legacy_allowed else "",
            message="denied" if not legacy_allowed else "",
            effective_group_id="20002" if scenario in {"private_target_group", "target_admin"} else "20001",
        )

    async def feature(group_id, name):
        return feature_enabled

    async def tool_switch(group_id, name):
        return tool_enabled

    async def admin(user_id, group_id, tool_context):
        return target_admin

    def record(**values):
        comparisons.append(values["comparison"])

    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", authorize)
    monkeypatch.setattr(agent_tool_access, "is_group_feature_enabled", feature)
    monkeypatch.setattr(agent_tool_access, "agent_tool_enabled_for_group", tool_switch)
    monkeypatch.setattr(agent_tool_access, "target_group_admin_authorized", admin)
    monkeypatch.setattr(gateway, "log_authorization_shadow_comparison", record)

    result = asyncio.run(gateway.execute_tool(tool.name, arguments, context))

    assert comparisons[0].category == expected_category, scenario
    assert handler_calls == expected_handler_calls
    if legacy_allowed:
        assert result["ok"] is True
    else:
        assert result["error"] == "tool_not_allowed"


def test_gateway_current_user_ownership_closes_reminder_gap(monkeypatch) -> None:
    handler_calls = 0
    comparisons = []

    async def handler(values, tool_context):
        nonlocal handler_calls
        handler_calls += 1
        return {"ok": True}

    tool = _tool("create_reminder", handler)
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)

    async def allow(tool_name, values, tool_context):
        return AgentToolAuthorization(True, effective_group_id="20001")

    async def enabled(*args):
        return True

    def record(**values):
        comparisons.append(values["comparison"])

    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", allow)
    monkeypatch.setattr(agent_tool_access, "is_group_feature_enabled", enabled)
    monkeypatch.setattr(agent_tool_access, "agent_tool_enabled_for_group", enabled)
    monkeypatch.setattr(gateway, "log_authorization_shadow_comparison", record)

    result = asyncio.run(
        gateway.execute_tool(
            "create_reminder",
            {},
            _context("group", "20001", _scope=SimpleNamespace(user_id="10001")),
        )
    )

    assert comparisons[0].category == "ALLOW_ALLOW"
    assert comparisons[0].reason_codes == ()
    assert result["ok"] is True
    assert handler_calls == 1


def test_gateway_permission_without_adapter_is_tightening_only(monkeypatch) -> None:
    comparisons = []

    async def handler(values, tool_context):
        return {"ok": True}

    tool = _tool("gateway_shadow_permission", handler)
    policy = resolve_authorization_policy_with_metadata(
        tool,
        AuthorizationMetadata(required_permissions=frozenset({"resource.owner"})),
    )
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    monkeypatch.setattr(gateway, "resolve_authorization_policy", lambda value: policy)

    async def allow(tool_name, values, tool_context):
        return AgentToolAuthorization(True)

    def record(**values):
        comparisons.append(values["comparison"])

    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", allow)
    monkeypatch.setattr(gateway, "log_authorization_shadow_comparison", record)

    result = asyncio.run(
        gateway.execute_tool(tool.name, {}, _context("private", "10001"))
    )

    assert comparisons[0].category == "LEGACY_ALLOW_METADATA_DENY"
    assert comparisons[0].reason_codes == ("permission_unresolved",)
    assert result["ok"] is True


def test_metadata_evaluator_failure_isolated_from_legacy_result(monkeypatch) -> None:
    handler_calls = 0

    async def handler(values, tool_context):
        nonlocal handler_calls
        handler_calls += 1
        return {"ok": True}

    tool = _tool("gateway_shadow_failure", handler)
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)

    async def allow(tool_name, values, tool_context):
        return AgentToolAuthorization(True)

    async def fail(**values):
        raise RuntimeError("metadata evaluator unavailable")

    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", allow)
    monkeypatch.setattr(gateway, "evaluate_metadata_authorization", fail)

    result = asyncio.run(
        gateway.execute_tool(tool.name, {}, _context("private", "10001"))
    )

    assert result["ok"] is True
    assert handler_calls == 1


def test_legacy_deny_metadata_allow_is_observed_but_not_enforced(monkeypatch) -> None:
    handler_calls = 0
    comparisons = []

    async def handler(values, tool_context):
        nonlocal handler_calls
        handler_calls += 1
        return {"ok": True}

    tool = _tool("gateway_shadow_p0_injection", handler)
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)

    async def deny(tool_name, values, tool_context):
        return AgentToolAuthorization(False, error="tool_not_allowed", message="denied")

    async def metadata_allow(**values):
        return ShadowAuthorizationResult(True)

    def record(**values):
        comparisons.append(values["comparison"])

    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", deny)
    monkeypatch.setattr(gateway, "evaluate_metadata_authorization", metadata_allow)
    monkeypatch.setattr(gateway, "log_authorization_shadow_comparison", record)

    result = asyncio.run(
        gateway.execute_tool(tool.name, {}, _context("private", "10001"))
    )

    assert comparisons[0].category == "LEGACY_DENY_METADATA_ALLOW"
    assert result["error"] == "tool_not_allowed"
    assert handler_calls == 0
