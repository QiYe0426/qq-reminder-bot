from __future__ import annotations

import asyncio
from collections import Counter

import pytest

from plugins import agent_tool_access
from plugins.agent_tool_access import AgentToolAuthorization
from plugins.agent_tools import gateway
from plugins.agent_tools.authorization_policy import (
    AuthorizationMetadata,
    resolve_authorization_policy_with_metadata,
)
from plugins.agent_tools.authorization_shadow import (
    AuthorizationDecisionComparison,
    ShadowAuthorizationResult,
    compare_authorization_decisions,
    evaluate_authorization_shadow,
    log_authorization_shadow_comparison,
)
from plugins.agent_tools.registry import AgentTool, AgentToolMetadata


def _definition(name: str) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Authorization shadow Runtime test tool.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    }


async def _handler(arguments, context):
    return {"ok": True}


def _tool(
    name: str,
    *,
    handler=_handler,
    requires_feature: str | None = None,
    requires_admin: bool = False,
    requires_group: bool = False,
    group_scope: str = "none",
    requires_target_group_admin: bool = False,
) -> AgentTool:
    return AgentTool(
        name=name,
        definition=_definition(name),
        handler=handler,
        requires_feature=requires_feature,
        requires_admin=requires_admin,
        requires_group=requires_group,
        group_scope=group_scope,
        requires_target_group_admin=requires_target_group_admin,
        metadata=AgentToolMetadata(resource_scope="target_group"),
    )


@pytest.mark.parametrize(
    ("scenario", "tool", "legacy_allowed", "context", "effective_group_id", "expected"),
    [
        (
            "current_group",
            _tool("shadow_current_group", group_scope="current"),
            True,
            {"_target_type": "group", "_target_id": "current"},
            "current",
            "ALLOW_ALLOW",
        ),
        (
            "private_explicit",
            _tool("shadow_private_explicit", group_scope="private_explicit"),
            True,
            {"_target_type": "private", "_target_id": "actor"},
            "target",
            "ALLOW_ALLOW",
        ),
        (
            "cross_group_id",
            _tool("shadow_cross_group", group_scope="current"),
            False,
            {"_target_type": "group", "_target_id": "current"},
            "",
            "DENY_DENY",
        ),
        (
            "target_group_admin",
            _tool("shadow_target_admin", requires_target_group_admin=True),
            False,
            {"_target_type": "private", "_target_id": "actor"},
            "target",
            "DENY_DENY",
        ),
        (
            "feature_disabled",
            _tool("shadow_feature_disabled", requires_feature="collector"),
            False,
            {"_target_type": "group", "_target_id": "current"},
            "current",
            "DENY_DENY",
        ),
        (
            "tool_switch_disabled",
            _tool("shadow_tool_switch_disabled"),
            False,
            {"_target_type": "group", "_target_id": "current"},
            "current",
            "DENY_DENY",
        ),
        (
            "authorization_api_failure",
            _tool("shadow_api_failure", requires_target_group_admin=True),
            False,
            {"_target_type": "private", "_target_id": "actor"},
            "target",
            "DENY_DENY",
        ),
    ],
)
def test_real_authorization_scenarios_compare_conservatively(
    scenario: str,
    tool: AgentTool,
    legacy_allowed: bool,
    context: dict[str, object],
    effective_group_id: str,
    expected: str,
) -> None:
    policy = resolve_authorization_policy_with_metadata(tool, AuthorizationMetadata())
    metadata_result = evaluate_authorization_shadow(
        policy,
        legacy_allowed=legacy_allowed,
        context=context,
        effective_group_id=effective_group_id,
    )
    comparison = compare_authorization_decisions(
        legacy_allowed=legacy_allowed,
        metadata_result=metadata_result,
        policy=policy,
    )

    assert comparison.category == expected, scenario
    assert comparison.category != "LEGACY_DENY_METADATA_ALLOW"


def test_reminder_ownership_remains_a_separate_tightening_boundary() -> None:
    tool = _tool("shadow_reminder_ownership")
    policy = resolve_authorization_policy_with_metadata(
        tool,
        AuthorizationMetadata(ownership_policy="current_user"),
    )

    metadata_result = evaluate_authorization_shadow(
        policy,
        legacy_allowed=True,
        context={"_target_type": "group", "_target_id": "current", "_user_id": "actor"},
        effective_group_id="",
    )
    comparison = compare_authorization_decisions(
        legacy_allowed=True,
        metadata_result=metadata_result,
        policy=policy,
    )

    assert comparison.category == "LEGACY_ALLOW_METADATA_DENY"
    assert metadata_result.reasons == ("unverified_ownership_policy",)


def test_gateway_legacy_denial_remains_authoritative(monkeypatch) -> None:
    handler_calls = 0
    observed: list[AuthorizationDecisionComparison] = []
    async def handler(arguments, context):
        nonlocal handler_calls
        handler_calls += 1
        return {"ok": True}

    tool = _tool("shadow_gateway_legacy_deny", handler=handler)
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)

    async def deny(tool_name, arguments, context):
        return AgentToolAuthorization(False, error="tool_not_allowed", message="denied")

    async def metadata_allow(**values):
        return ShadowAuthorizationResult(True)

    def record(**values):
        comparison = values["comparison"]
        observed.append(comparison)

    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", deny)
    monkeypatch.setattr(gateway, "evaluate_metadata_authorization", metadata_allow)
    monkeypatch.setattr(gateway, "log_authorization_shadow_comparison", record)

    result = asyncio.run(
        gateway.execute_tool(
            tool.name,
            {},
            {"_target_type": "group", "_target_id": "current", "_user_id": "actor"},
        )
    )

    assert observed[0].category == "LEGACY_DENY_METADATA_ALLOW"
    assert result["error"] == "tool_not_allowed"
    assert handler_calls == 0


def test_gateway_legacy_allow_remains_authoritative(monkeypatch) -> None:
    handler_calls = 0
    async def handler(arguments, context):
        nonlocal handler_calls
        handler_calls += 1
        return {"ok": True}

    tool = _tool("shadow_gateway_legacy_allow", handler=handler)
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)

    async def allow(tool_name, arguments, context):
        return AgentToolAuthorization(True, effective_group_id="current")

    async def metadata_deny(**values):
        return ShadowAuthorizationResult(False, ("test_tightening",))

    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", allow)
    monkeypatch.setattr(gateway, "evaluate_metadata_authorization", metadata_deny)

    result = asyncio.run(
        gateway.execute_tool(
            tool.name,
            {},
            {"_target_type": "group", "_target_id": "current", "_user_id": "actor"},
        )
    )

    assert result["ok"] is True
    assert handler_calls == 1


def test_shadow_observation_failure_does_not_change_gateway_result(monkeypatch) -> None:
    tool = _tool("shadow_gateway_observer_failure")
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)

    async def allow(tool_name, arguments, context):
        return AgentToolAuthorization(True)

    def fail(*args, **values):
        raise RuntimeError("shadow failure")

    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", allow)
    monkeypatch.setattr(gateway, "resolve_authorization_policy", fail)

    result = asyncio.run(
        gateway.execute_tool(
            tool.name,
            {},
            {"_target_type": "private", "_target_id": "actor", "_user_id": "actor"},
        )
    )

    assert result["ok"] is True


class _FakeLogger:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple[object, ...]]] = []

    def warning(self, message: str, *args: object) -> None:
        self.calls.append(("warning", message, args))

    def info(self, message: str, *args: object) -> None:
        self.calls.append(("info", message, args))

    def debug(self, message: str, *args: object) -> None:
        self.calls.append(("debug", message, args))


def test_p0_comparison_warns_without_sensitive_context(monkeypatch) -> None:
    from plugins.agent_tools import authorization_shadow

    fake = _FakeLogger()
    monkeypatch.setattr(authorization_shadow, "logger", fake)
    comparison = AuthorizationDecisionComparison(
        category="LEGACY_DENY_METADATA_ALLOW",
        legacy_allowed=False,
        metadata_allowed=True,
        conflict_fields=("admin_requirements",),
        reason_codes=("test_reason",),
    )

    log_authorization_shadow_comparison(
        invocation_id="invocation-safe",
        tool_name="tool-safe",
        comparison=comparison,
    )

    assert fake.calls[0][0] == "warning"
    rendered = f"{fake.calls[0][1]} {fake.calls[0][2]}"
    assert "invocation-safe" in rendered
    assert "tool-safe" in rendered
    assert "group_id" not in rendered
    assert "_user_id" not in rendered
    assert "arguments" not in rendered


def test_shadow_scenario_decision_inventory() -> None:
    categories = Counter(
        ["ALLOW_ALLOW", "ALLOW_ALLOW"]
        + ["DENY_DENY"] * 5
        + ["LEGACY_ALLOW_METADATA_DENY"]
    )

    assert categories == Counter(
        {
            "ALLOW_ALLOW": 2,
            "DENY_DENY": 5,
            "LEGACY_ALLOW_METADATA_DENY": 1,
            "LEGACY_DENY_METADATA_ALLOW": 0,
        }
    )
