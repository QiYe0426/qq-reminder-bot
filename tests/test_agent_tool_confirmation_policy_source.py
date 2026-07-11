from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from plugins import agent_tool_access
from plugins.agent_tool_access import AgentToolAuthorization
from plugins.agent_tools import confirmation, gateway
from plugins.agent_tools.confirmation import confirm_pending_confirmation
from plugins.agent_tools.registry import AgentTool, AgentToolMetadata


def _definition(name: str) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Confirmation policy source test tool.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    }


def _context(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "_user_id": "confirmation-policy-user",
        "_target_type": "group",
        "_target_id": "confirmation-policy-group",
        "_tool_call_id": "confirmation-policy-call",
        "_invocation_source": "agent",
    }
    value.update(updates)
    return value


@pytest.fixture
def confirmation_db(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(confirmation, "DB_PATH", tmp_path / "agent_tool_confirmations.db")


def _allow(monkeypatch, calls: list[str] | None = None) -> None:
    async def authorize(tool_name, arguments, context):
        if calls is not None:
            calls.append(tool_name)
        return AgentToolAuthorization(True, effective_group_id="confirmation-policy-group")

    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", authorize)


def _install_tool(monkeypatch, tool: AgentTool) -> None:
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool if name == tool.name else None)


def test_required_metadata_enhances_confirmation_and_preserves_token_lifecycle(
    confirmation_db, monkeypatch
) -> None:
    handler_calls = 0

    async def handler(arguments, context):
        nonlocal handler_calls
        handler_calls += 1
        return {"ok": True}

    tool = AgentTool(
        name="confirmation_required_from_metadata",
        definition=_definition("confirmation_required_from_metadata"),
        handler=handler,
        requires_confirmation=False,
        confirmation_timeout=37,
        metadata=AgentToolMetadata(confirmation_policy="required", timeout_seconds=999),
    )
    _install_tool(monkeypatch, tool)
    _allow(monkeypatch)

    async def run():
        pending = await gateway.execute_tool(tool.name, {}, _context())
        approved = await confirm_pending_confirmation(
            str(pending["data"]["confirmation_code"]),
            user_id="confirmation-policy-user",
            target_type="group",
            target_id="confirmation-policy-group",
        )
        approved_context = _context(_tool_confirmation_token=approved.token)
        executed = await gateway.execute_tool(tool.name, {}, approved_context)
        replay = await gateway.execute_tool(tool.name, {}, approved_context)
        return pending, approved, executed, replay

    pending, approved, executed, replay = asyncio.run(run())

    assert pending["error"] == "confirmation_required"
    assert pending["data"]["confirmation_code"]
    assert approved.ok is True
    assert approved.confirmation is not None
    assert approved.confirmation.confirmation_id == pending["data"].get("confirmation_id", approved.confirmation.confirmation_id)
    assert approved.token
    assert executed["ok"] is True
    assert replay["error"] == "confirmation_consumed"
    assert handler_calls == 1
    assert tool.confirmation_timeout == 37
    assert tool.metadata.timeout_seconds == 999


def test_confirmation_expiration_is_unchanged(confirmation_db, monkeypatch) -> None:
    async def handler(arguments, context):
        raise AssertionError("Expired confirmation must not invoke the handler")

    tool = AgentTool(
        name="confirmation_expiration_from_metadata",
        definition=_definition("confirmation_expiration_from_metadata"),
        handler=handler,
        confirmation_timeout=1,
        metadata=AgentToolMetadata(confirmation_policy="required"),
    )
    _install_tool(monkeypatch, tool)
    _allow(monkeypatch)

    pending = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))
    monkeypatch.setattr(
        confirmation,
        "now_utc",
        lambda: datetime.now(timezone.utc) + timedelta(days=1),
    )
    expired = asyncio.run(
        confirm_pending_confirmation(
            str(pending["data"]["confirmation_code"]),
            user_id="confirmation-policy-user",
            target_type="group",
            target_id="confirmation-policy-group",
        )
    )

    assert expired.ok is False
    assert expired.error == "confirmation_expired"


@pytest.mark.parametrize(
    ("name", "metadata_policy", "legacy_required", "expects_confirmation"),
    [
        ("confirmation_never_keeps_legacy", "never", True, True),
        ("confirmation_optional_fallback", "optional", False, False),
        ("confirmation_conditional_fallback", "conditional", True, True),
    ],
)
def test_confirmation_policy_resolution_cases(
    confirmation_db,
    monkeypatch,
    name: str,
    metadata_policy: str,
    legacy_required: bool,
    expects_confirmation: bool,
) -> None:
    handler_calls = 0

    async def handler(arguments, context):
        nonlocal handler_calls
        handler_calls += 1
        return {"ok": True}

    tool = AgentTool(
        name=name,
        definition=_definition(name),
        handler=handler,
        requires_confirmation=legacy_required,
        metadata=AgentToolMetadata(confirmation_policy=metadata_policy),
    )
    _install_tool(monkeypatch, tool)
    _allow(monkeypatch)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    if expects_confirmation:
        assert result["error"] == "confirmation_required"
        assert handler_calls == 0
    else:
        assert result["ok"] is True
        assert handler_calls == 1


def test_authorization_denial_still_precedes_confirmation(monkeypatch) -> None:
    order: list[str] = []

    async def handler(arguments, context):
        order.append("handler")
        return {"ok": True}

    tool = AgentTool(
        name="confirmation_authorization_order",
        definition=_definition("confirmation_authorization_order"),
        handler=handler,
        metadata=AgentToolMetadata(confirmation_policy="required"),
    )
    _install_tool(monkeypatch, tool)

    async def deny(tool_name, arguments, context):
        order.append("authorization")
        return AgentToolAuthorization(False, error="group_permission_denied", message="denied")

    async def unexpected_confirmation(*args, **kwargs):
        order.append("confirmation")
        raise AssertionError("Confirmation must not run after authorization denial")

    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", deny)
    monkeypatch.setattr(gateway, "create_pending_confirmation", unexpected_confirmation)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context()))

    assert result["error"] == "group_permission_denied"
    assert order == ["authorization"]
