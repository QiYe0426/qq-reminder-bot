import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from plugins import agent_tool_access
from plugins.agent_tool_access import AgentToolAuthorization
from plugins.agent_tools import AgentTool, execute_tool, register_tool
from plugins.agent_tools import confirmation
from plugins.agent_tools.confirmation import confirm_pending_confirmation


def tool_definition(name: str) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Confirmation test tool.",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    }


@pytest.fixture
def confirmation_db(tmp_path, monkeypatch):
    monkeypatch.setattr(confirmation, "DB_PATH", tmp_path / "agent_tool_confirmations.db")


def allow_all_tools(monkeypatch, *, effective_group_id: str = "") -> None:
    async def authorize(
        tool_name: str,
        arguments: dict[str, object],
        context: dict[str, object],
    ) -> AgentToolAuthorization:
        return AgentToolAuthorization(True, effective_group_id=effective_group_id)

    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", authorize)


def high_risk_tool(name: str, handler) -> AgentTool:
    return AgentTool(
        name=name,
        definition=tool_definition(name),
        handler=handler,
        side_effect="write",
        risk_level="high",
        requires_confirmation=True,
        confirmation_timeout=120,
    )


def confirmation_context(**updates: object) -> dict[str, object]:
    context: dict[str, object] = {
        "_user_id": "user-1",
        "_target_type": "group",
        "_target_id": "group-1",
    }
    context.update(updates)
    return context


async def approve_result(result: dict[str, object], context: dict[str, object]) -> str:
    data = result.get("data")
    assert isinstance(data, dict)
    approval = await confirm_pending_confirmation(
        str(data["confirmation_code"]),
        user_id=str(context.get("_user_id") or ""),
        target_type=str(context.get("_target_type") or ""),
        target_id=str(context.get("_target_id") or ""),
    )
    assert approval.ok is True
    assert approval.token
    return approval.token


def test_high_risk_tool_does_not_execute_without_confirmation(confirmation_db, monkeypatch) -> None:
    called = False

    async def handler(args, context):
        nonlocal called
        called = True
        return {"ok": True}

    allow_all_tools(monkeypatch, effective_group_id="group-1")
    name = "confirmation_required_test"
    register_tool(high_risk_tool(name, handler))

    result = asyncio.run(execute_tool(name, {"value": "original"}, confirmation_context()))

    assert result["ok"] is False
    assert result["error"] == "confirmation_required"
    assert called is False
    assert "confirmation_code" in result["data"]
    assert "confirmation_id" not in result["data"]
    assert "token" not in result["data"]


def test_each_high_risk_call_creates_a_distinct_pending_confirmation(confirmation_db, monkeypatch) -> None:
    async def handler(args, context):
        return {"ok": True}

    allow_all_tools(monkeypatch)
    name = "confirmation_no_pending_reuse_test"
    register_tool(high_risk_tool(name, handler))

    async def run() -> tuple[dict[str, object], dict[str, object]]:
        first = await execute_tool(name, {"value": "same"}, confirmation_context())
        second = await execute_tool(name, {"value": "same"}, confirmation_context())
        return first, second

    first, second = asyncio.run(run())

    assert first["data"]["confirmation_code"] != second["data"]["confirmation_code"]


def test_confirmed_tool_executes_exact_saved_call(confirmation_db, monkeypatch) -> None:
    received: list[tuple[dict[str, object], dict[str, object]]] = []

    async def handler(args, context):
        received.append((dict(args), dict(context)))
        return {"ok": True, "message": "executed", "value": args["value"]}

    allow_all_tools(monkeypatch, effective_group_id="group-1")
    name = "confirmation_execute_test"
    register_tool(high_risk_tool(name, handler))
    context = confirmation_context()

    async def run() -> dict[str, object]:
        pending = await execute_tool(name, {"value": "original"}, context)
        token = await approve_result(pending, context)
        return await execute_tool(
            name,
            {"value": "original"},
            {**context, "_tool_confirmation_token": token},
        )

    result = asyncio.run(run())

    assert result["ok"] is True
    assert result["data"]["value"] == "original"
    assert received[0][0] == {"value": "original"}
    assert "_tool_confirmation_token" not in received[0][1]


def test_expired_token_is_rejected(confirmation_db, monkeypatch) -> None:
    called = False
    clock = datetime(2026, 7, 11, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(confirmation, "now_utc", lambda: clock)

    async def handler(args, context):
        nonlocal called
        called = True
        return {"ok": True}

    allow_all_tools(monkeypatch)
    name = "confirmation_expired_test"
    register_tool(high_risk_tool(name, handler))
    context = confirmation_context()

    async def run() -> dict[str, object]:
        nonlocal clock
        pending = await execute_tool(name, {"value": "original"}, context)
        token = await approve_result(pending, context)
        clock += timedelta(minutes=3)
        return await execute_tool(name, {"value": "original"}, {**context, "_tool_confirmation_token": token})

    result = asyncio.run(run())

    assert result["error"] == "confirmation_expired"
    assert called is False


def test_consumed_token_cannot_be_replayed(confirmation_db, monkeypatch) -> None:
    calls = 0

    async def handler(args, context):
        nonlocal calls
        calls += 1
        return {"ok": True}

    allow_all_tools(monkeypatch)
    name = "confirmation_replay_test"
    register_tool(high_risk_tool(name, handler))
    context = confirmation_context()

    async def run() -> tuple[dict[str, object], dict[str, object]]:
        pending = await execute_tool(name, {"value": "original"}, context)
        token = await approve_result(pending, context)
        confirmed_context = {**context, "_tool_confirmation_token": token}
        first = await execute_tool(name, {"value": "original"}, confirmed_context)
        second = await execute_tool(name, {"value": "original"}, confirmed_context)
        return first, second

    first, second = asyncio.run(run())

    assert first["ok"] is True
    assert second["error"] == "confirmation_consumed"
    assert calls == 1


@pytest.mark.parametrize(
    ("changed_arguments", "context_updates"),
    [
        ({"value": "changed"}, {}),
        ({"value": "original"}, {"_user_id": "user-2"}),
        ({"value": "original"}, {"_target_id": "group-2"}),
    ],
)
def test_confirmation_binding_mismatch_is_rejected(
    confirmation_db,
    monkeypatch,
    changed_arguments: dict[str, object],
    context_updates: dict[str, object],
) -> None:
    called = False

    async def handler(args, context):
        nonlocal called
        called = True
        return {"ok": True}

    allow_all_tools(monkeypatch, effective_group_id="effective-group")
    name = f"confirmation_mismatch_{changed_arguments['value']}_{len(context_updates)}_{next(iter(context_updates.values()), 'none')}"
    register_tool(high_risk_tool(name, handler))
    context = confirmation_context()

    async def run() -> dict[str, object]:
        pending = await execute_tool(name, {"value": "original"}, context)
        token = await approve_result(pending, context)
        changed_context = {**context, **context_updates, "_tool_confirmation_token": token}
        return await execute_tool(name, changed_arguments, changed_context)

    result = asyncio.run(run())

    assert result["error"] == "confirmation_mismatch"
    assert called is False


def test_permission_is_checked_again_before_token_consumption(confirmation_db, monkeypatch) -> None:
    called = False
    allowed = True

    async def authorize(tool_name, arguments, context):
        if allowed:
            return AgentToolAuthorization(True, effective_group_id="group-1")
        return AgentToolAuthorization(False, error="group_permission_denied", message="permission revoked")

    async def handler(args, context):
        nonlocal called
        called = True
        return {"ok": True}

    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", authorize)
    name = "confirmation_permission_revoked_test"
    register_tool(high_risk_tool(name, handler))
    context = confirmation_context()

    async def run() -> dict[str, object]:
        nonlocal allowed
        pending = await execute_tool(name, {"value": "original"}, context)
        token = await approve_result(pending, context)
        allowed = False
        return await execute_tool(name, {"value": "original"}, {**context, "_tool_confirmation_token": token})

    result = asyncio.run(run())

    assert result["error"] == "group_permission_denied"
    assert called is False


def test_low_risk_tool_executes_without_confirmation(confirmation_db, monkeypatch) -> None:
    called = False

    async def handler(args, context):
        nonlocal called
        called = True
        return {"ok": True, "value": args["value"]}

    allow_all_tools(monkeypatch)
    name = "confirmation_low_risk_test"
    register_tool(AgentTool(name=name, definition=tool_definition(name), handler=handler))

    result = asyncio.run(execute_tool(name, {"value": "read"}, confirmation_context()))

    assert result["ok"] is True
    assert called is True


def test_agent_stops_after_confirmation_required(monkeypatch) -> None:
    import nonebot

    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    from plugins import ai_chat

    calls: list[str] = []

    class FakeEvent:
        user_id = 10001

    async def build_prompt(extra_context: str = "") -> str:
        return "system"

    async def filter_tools(definitions, context):
        return definitions

    async def create_response(*args, **kwargs):
        tool_calls = [
            SimpleNamespace(
                id="call-1",
                type="function",
                function=SimpleNamespace(name="set_group_features", arguments='{"group_id":"1001","collector":true}'),
            ),
            SimpleNamespace(
                id="call-2",
                type="function",
                function=SimpleNamespace(name="get_chime", arguments="{}"),
            ),
        ]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=tool_calls, content=""))])

    async def run_tool(name, args, context):
        calls.append(name)
        return {
            "ok": False,
            "error": "confirmation_required",
            "message": "confirm this action",
            "retryable": False,
        }

    monkeypatch.setattr(ai_chat, "build_agent_system_prompt", build_prompt)
    monkeypatch.setattr(ai_chat, "filter_allowed_agent_tool_definitions", filter_tools)
    monkeypatch.setattr(ai_chat, "create_chat_response", create_response)
    monkeypatch.setattr(ai_chat, "run_agent_tool", run_tool)

    result = asyncio.run(ai_chat.ask_ai_with_agent("change settings", event=FakeEvent()))

    assert result == "confirm this action"
    assert calls == ["set_group_features"]


def test_ai_chat_confirmation_executes_saved_call_directly(confirmation_db, monkeypatch) -> None:
    import nonebot

    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    from plugins import ai_chat

    received: list[dict[str, object]] = []

    async def handler(args, context):
        received.append(dict(args))
        return {"ok": True, "message": "saved call executed"}

    allow_all_tools(monkeypatch)
    name = "confirmation_ai_chat_replay_test"
    register_tool(high_risk_tool(name, handler))
    context = {
        "_user_id": "10001",
        "_target_type": "private",
        "_target_id": "10001",
    }

    class FakePrivateEvent:
        user_id = 10001

        def __init__(self, text: str) -> None:
            self.text = text

        def get_plaintext(self) -> str:
            return self.text

    async def run() -> str | None:
        pending = await execute_tool(name, {"value": "server-saved"}, context)
        code = pending["data"]["confirmation_code"]
        return await ai_chat.handle_tool_confirmation_reply(FakePrivateEvent(f"确认 {code}"))

    reply = asyncio.run(run())

    assert reply == "saved call executed"
    assert received == [{"value": "server-saved"}]
