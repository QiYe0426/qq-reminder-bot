from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from plugins import agent_tool_access
from plugins.agent_tool_access import AgentToolAuthorization
from plugins.agent_tools import gateway
from plugins.agent_tools.authorization_shadow import (
    AUTHORIZATION_V2_ENFORCEMENT_ENV,
    MetadataAuthorizationDecision,
    authorization_v2_enforcement_enabled,
)
from plugins.agent_tools.registry import AgentTool, AgentToolMetadata


def _definition(name: str) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Authorization v2 enforcement test tool.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    }


def _tool(name: str, handler, **updates: object) -> AgentTool:
    return AgentTool(
        name=name,
        definition=_definition(name),
        handler=handler,
        metadata=AgentToolMetadata(),
        **updates,
    )


def _context() -> dict[str, object]:
    return {
        "_target_type": "private",
        "_target_id": "actor-session",
        "_user_id": "actor",
        "_tool_call_id": "enforcement-call",
        "_invocation_source": "agent",
    }


def _metadata_decision(allowed: bool, *reasons: str) -> MetadataAuthorizationDecision:
    return MetadataAuthorizationDecision(
        allowed=allowed,
        reason_codes=tuple(reasons),
        effective_group_id="",
        failed_requirements=tuple(reasons),
        conflicts=(),
    )


def _install_decisions(
    monkeypatch,
    tool: AgentTool,
    *,
    legacy_allowed: bool,
    metadata_allowed: bool,
    metadata_reasons: tuple[str, ...] = (),
) -> None:
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)

    async def authorize(tool_name, arguments, context):
        return AgentToolAuthorization(
            legacy_allowed,
            error="group_permission_denied" if not legacy_allowed else "",
            message="legacy denied" if not legacy_allowed else "",
        )

    async def metadata(**values):
        return _metadata_decision(metadata_allowed, *metadata_reasons)

    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", authorize)
    monkeypatch.setattr(gateway, "evaluate_metadata_authorization", metadata)
    monkeypatch.setattr(gateway, "log_authorization_shadow_comparison", lambda **values: None)


@pytest.mark.parametrize(
    ("legacy_allowed", "metadata_allowed", "expected_ok", "expected_error", "handler_calls"),
    [
        (True, True, True, None, 1),
        (True, False, False, "tool_not_allowed", 0),
        (False, True, False, "group_permission_denied", 0),
        (False, False, False, "group_permission_denied", 0),
    ],
)
def test_legacy_and_metadata_enforcement_matrix(
    monkeypatch,
    legacy_allowed: bool,
    metadata_allowed: bool,
    expected_ok: bool,
    expected_error: str | None,
    handler_calls: int,
) -> None:
    calls = 0

    async def handler(arguments, context):
        nonlocal calls
        calls += 1
        return {"ok": True}

    tool = _tool("authorization_enforcement_matrix", handler)
    _install_decisions(
        monkeypatch,
        tool,
        legacy_allowed=legacy_allowed,
        metadata_allowed=metadata_allowed,
    )
    monkeypatch.setattr(gateway, "authorization_v2_enforcement_enabled", lambda: True)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    assert result["ok"] is expected_ok
    assert result.get("error") == expected_error
    assert calls == handler_calls


def test_flag_false_rolls_back_to_legacy_allow(monkeypatch) -> None:
    calls = 0

    async def handler(arguments, context):
        nonlocal calls
        calls += 1
        return {"ok": True}

    tool = _tool("authorization_enforcement_rollback", handler)
    _install_decisions(
        monkeypatch,
        tool,
        legacy_allowed=True,
        metadata_allowed=False,
        metadata_reasons=("metadata_would_deny",),
    )
    monkeypatch.setattr(gateway, "authorization_v2_enforcement_enabled", lambda: False)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    assert result["ok"] is True
    assert calls == 1


def test_feature_flag_defaults_false_and_accepts_explicit_truthy_values(monkeypatch) -> None:
    monkeypatch.delenv(AUTHORIZATION_V2_ENFORCEMENT_ENV, raising=False)
    assert authorization_v2_enforcement_enabled() is False

    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv(AUTHORIZATION_V2_ENFORCEMENT_ENV, value)
        assert authorization_v2_enforcement_enabled() is True

    monkeypatch.setenv(AUTHORIZATION_V2_ENFORCEMENT_ENV, "0")
    assert authorization_v2_enforcement_enabled() is False


def test_metadata_denial_stops_before_confirmation_idempotency_and_handler(monkeypatch) -> None:
    downstream: list[str] = []

    async def handler(arguments, context):
        downstream.append("handler")
        return {"ok": True}

    tool = _tool(
        "authorization_enforcement_short_circuit",
        handler,
        requires_confirmation=True,
        idempotency_enabled=True,
    )
    _install_decisions(
        monkeypatch,
        tool,
        legacy_allowed=True,
        metadata_allowed=False,
    )
    monkeypatch.setattr(gateway, "authorization_v2_enforcement_enabled", lambda: True)

    async def confirmation(*args, **kwargs):
        downstream.append("confirmation")
        raise AssertionError("Confirmation must not run after Authorization denial")

    async def claim(*args, **kwargs):
        downstream.append("idempotency")
        raise AssertionError("Idempotency must not run after Authorization denial")

    monkeypatch.setattr(gateway, "create_pending_confirmation", confirmation)
    monkeypatch.setattr(gateway, "claim_execution", claim)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    assert result["error"] == "tool_not_allowed"
    assert downstream == []


def test_confirmation_still_runs_after_both_authorizations_allow(monkeypatch) -> None:
    order: list[str] = []

    async def handler(arguments, context):
        order.append("handler")
        return {"ok": True}

    tool = _tool(
        "authorization_enforcement_confirmation_order",
        handler,
        requires_confirmation=True,
    )
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)

    async def authorize(tool_name, arguments, context):
        order.append("legacy_authorization")
        return AgentToolAuthorization(True)

    async def metadata(**values):
        order.append("metadata_authorization")
        return _metadata_decision(True)

    async def confirmation(**values):
        order.append("confirmation")
        return SimpleNamespace(
            confirmation_id=1,
            confirmation_code="ABCDEFGH",
            arguments_hash="hash",
            expires_at="2099-01-01T00:00:00+00:00",
            status="pending",
        )

    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", authorize)
    monkeypatch.setattr(gateway, "evaluate_metadata_authorization", metadata)
    monkeypatch.setattr(gateway, "create_pending_confirmation", confirmation)
    monkeypatch.setattr(gateway, "authorization_v2_enforcement_enabled", lambda: True)
    monkeypatch.setattr(gateway, "log_authorization_shadow_comparison", lambda **values: None)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    assert result["error"] == "confirmation_required"
    assert order == ["legacy_authorization", "metadata_authorization", "confirmation"]


def test_idempotency_still_runs_after_both_authorizations_allow(monkeypatch) -> None:
    order: list[str] = []

    async def handler(arguments, context):
        order.append("handler")
        return {"ok": True}

    tool = _tool(
        "authorization_enforcement_idempotency_order",
        handler,
        idempotency_enabled=True,
    )
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)

    async def authorize(tool_name, arguments, context):
        order.append("legacy_authorization")
        return AgentToolAuthorization(True)

    async def metadata(**values):
        order.append("metadata_authorization")
        return _metadata_decision(True)

    def binding(**values):
        return SimpleNamespace(idempotency_key="key")

    async def claim(*args, **kwargs):
        order.append("idempotency_claim")
        return SimpleNamespace(action="execute", owner_token="owner", record=None)

    async def complete(*args, **kwargs):
        order.append("idempotency_complete")
        return True

    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", authorize)
    monkeypatch.setattr(gateway, "evaluate_metadata_authorization", metadata)
    monkeypatch.setattr(gateway, "create_execution_binding", binding)
    monkeypatch.setattr(gateway, "claim_execution", claim)
    monkeypatch.setattr(gateway, "complete_execution", complete)
    monkeypatch.setattr(gateway, "authorization_v2_enforcement_enabled", lambda: True)
    monkeypatch.setattr(gateway, "log_authorization_shadow_comparison", lambda **values: None)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    assert result["ok"] is True
    assert order == [
        "legacy_authorization",
        "metadata_authorization",
        "idempotency_claim",
        "handler",
        "idempotency_complete",
    ]


def test_legacy_error_code_and_message_are_unchanged(monkeypatch) -> None:
    async def handler(arguments, context):
        raise AssertionError("Legacy denial must not invoke Handler")

    tool = _tool("authorization_enforcement_legacy_error", handler)
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)

    async def deny(tool_name, arguments, context):
        return AgentToolAuthorization(
            False,
            error="feature_disabled",
            message="legacy feature message",
        )

    async def metadata(**values):
        return _metadata_decision(True)

    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", deny)
    monkeypatch.setattr(gateway, "evaluate_metadata_authorization", metadata)
    monkeypatch.setattr(gateway, "authorization_v2_enforcement_enabled", lambda: True)
    monkeypatch.setattr(gateway, "log_authorization_shadow_comparison", lambda **values: None)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    assert result["error"] == "feature_disabled"
    assert result["message"] == "legacy feature message"


def test_metadata_denial_uses_generic_non_leaking_error(monkeypatch) -> None:
    async def handler(arguments, context):
        raise AssertionError("Metadata denial must not invoke Handler")

    tool = _tool("authorization_enforcement_metadata_error", handler)
    secret_reason = "internal_policy_secret_actor_and_group"
    _install_decisions(
        monkeypatch,
        tool,
        legacy_allowed=True,
        metadata_allowed=False,
        metadata_reasons=(secret_reason,),
    )
    monkeypatch.setattr(gateway, "authorization_v2_enforcement_enabled", lambda: True)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    assert result == {
        "ok": False,
        "error": "tool_not_allowed",
        "message": "工具授权未通过。",
        "retryable": False,
    }
    assert secret_reason not in str(result)


@pytest.mark.parametrize("enforcement", [False, True])
def test_metadata_evaluator_failure_respects_rollback_mode(monkeypatch, enforcement: bool) -> None:
    calls = 0

    async def handler(arguments, context):
        nonlocal calls
        calls += 1
        return {"ok": True}

    tool = _tool("authorization_enforcement_evaluator_failure", handler)
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)

    async def allow(tool_name, arguments, context):
        return AgentToolAuthorization(True)

    async def fail(**values):
        raise RuntimeError("metadata engine unavailable")

    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", allow)
    monkeypatch.setattr(gateway, "evaluate_metadata_authorization", fail)
    monkeypatch.setattr(
        gateway,
        "authorization_v2_enforcement_enabled",
        lambda: enforcement,
    )

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    if enforcement:
        assert result["error"] == "tool_not_allowed"
        assert calls == 0
    else:
        assert result["ok"] is True
        assert calls == 1
