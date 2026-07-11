from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque
from types import SimpleNamespace

import aiosqlite
import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from plugins import agent_tool_access, ai_chat
from plugins.agent_tool_access import AgentToolAuthorization
from plugins.agent_tools import AgentTool, register_tool
from plugins.agent_tools import audit, confirmation


class FakePrivateEvent:
    user_id = 10001

    def __init__(self, text: str = "") -> None:
        self.text = text

    def get_plaintext(self) -> str:
        return self.text


def tool_definition(name: str) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Runtime audit test tool.",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    }


def tool_response(name: str, arguments: dict[str, object], call_id: str):
    call = SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )
    message = SimpleNamespace(tool_calls=[call], content="")
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def text_response(content: str = "done"):
    message = SimpleNamespace(tool_calls=[], content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def allow_all(monkeypatch) -> None:
    async def allowed(tool_name, context):
        return True, ""

    async def authorized(tool_name, arguments, context):
        return AgentToolAuthorization(True)

    async def filter_tools(definitions, context):
        return definitions

    async def build_prompt(extra_context: str = "") -> str:
        return "system"

    monkeypatch.setattr(ai_chat, "is_agent_tool_allowed", allowed)
    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", authorized)
    monkeypatch.setattr(ai_chat, "filter_allowed_agent_tool_definitions", filter_tools)
    monkeypatch.setattr(ai_chat, "build_agent_system_prompt", build_prompt)


async def audit_rows(tool_name: str) -> list[aiosqlite.Row]:
    async with aiosqlite.connect(audit.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM agent_tool_audit_events
            WHERE tool_name = ?
            ORDER BY occurred_at, invocation_id, sequence
            """,
            (tool_name,),
        )
        return await cursor.fetchall()


def grouped_events(rows: list[aiosqlite.Row]) -> list[list[aiosqlite.Row]]:
    groups: dict[str, list[aiosqlite.Row]] = defaultdict(list)
    for row in rows:
        groups[str(row["invocation_id"])].append(row)
    return [sorted(group, key=lambda row: int(row["sequence"])) for group in groups.values()]


def test_web_search_audit_keeps_only_query_fingerprint_and_result_count(monkeypatch) -> None:
    query = "private search phrase 123"

    async def search(value, max_results):
        assert value == query
        return {"query": value, "results": [{"title": "one"}, {"title": "two"}], "error": ""}

    allow_all(monkeypatch)
    monkeypatch.setattr(ai_chat, "agent_web_search", search)

    result = asyncio.run(
        ai_chat.run_agent_tool(
            "web_search",
            {"query": query, "max_results": 2},
            {"_user_id": "10001", "_target_type": "private", "_target_id": "10001", "_tool_call_id": "web-1"},
        )
    )
    rows = asyncio.run(audit_rows("web_search"))
    raw_rows = json.dumps([dict(row) for row in rows], ensure_ascii=False)

    assert len(result["results"]) == 2
    assert [row["event_type"] for row in rows] == ["tool_requested", "execution_started", "execution_completed"]
    assert all(row["tool_call_id"] == "web-1" for row in rows)
    assert all(row["arguments_fingerprint"] for row in rows)
    assert json.loads(rows[-1]["safe_details_json"])["result_count"] == 2
    assert query not in raw_rows


def test_fetch_url_audit_never_stores_full_url_or_query(monkeypatch) -> None:
    url = "https://private.example:8443/path?token=secret-value#fragment"

    async def fetch(value):
        assert value == url
        return {"url": value, "title": "title", "content": "123456", "error": ""}

    allow_all(monkeypatch)
    monkeypatch.setattr(ai_chat, "fetch_url_for_agent", fetch)

    asyncio.run(
        ai_chat.run_agent_tool(
            "fetch_url",
            {"url": url},
            {"_user_id": "10001", "_target_type": "private", "_target_id": "10001", "_tool_call_id": "fetch-1"},
        )
    )
    rows = asyncio.run(audit_rows("fetch_url"))
    details = json.loads(rows[-1]["safe_details_json"])
    raw_rows = json.dumps([dict(row) for row in rows], ensure_ascii=False)

    assert details["scheme"] == "https"
    assert details["port"] == 8443
    assert details["host_fingerprint"]
    assert details["result_size"] == 6
    assert "private.example" not in raw_rows
    assert "secret-value" not in raw_rows
    assert url not in raw_rows


def test_get_chime_writes_requested_started_and_completed(monkeypatch) -> None:
    async def get_state(target_type, target_id):
        return {"enabled": True, "mode": "hourly"}

    allow_all(monkeypatch)
    monkeypatch.setattr(ai_chat, "get_chime_state", get_state)

    asyncio.run(
        ai_chat.run_agent_tool(
            "get_chime",
            {},
            {"_user_id": "10001", "_target_type": "group", "_target_id": "20001", "_tool_call_id": "chime-1"},
        )
    )
    rows = asyncio.run(audit_rows("get_chime"))

    assert [row["event_type"] for row in rows] == ["tool_requested", "execution_started", "execution_completed"]
    assert all(row["tool_call_id"] == "chime-1" for row in rows)


def test_respond_emits_audit_without_reply_content(monkeypatch) -> None:
    message = "private final response content"
    allow_all(monkeypatch)

    async def create_response(*args, **kwargs):
        return tool_response("respond", {"message": message, "sources": []}, "respond-1")

    monkeypatch.setattr(ai_chat, "create_chat_response", create_response)

    result = asyncio.run(ai_chat.ask_ai_with_agent("question", event=FakePrivateEvent()))
    rows = asyncio.run(audit_rows("respond"))
    raw_rows = json.dumps([dict(row) for row in rows], ensure_ascii=False)

    assert result == message
    assert [row["event_type"] for row in rows] == ["response_emitted"]
    assert rows[0]["tool_call_id"] == "respond-1"
    assert json.loads(rows[0]["safe_details_json"])["response_length"] == len(message)
    assert message not in raw_rows


def test_registered_tool_call_id_reaches_full_gateway_chain(monkeypatch) -> None:
    async def handler(arguments, context):
        return {"ok": True, "value": arguments["value"]}

    allow_all(monkeypatch)
    name = "runtime_audit_call_id_test"
    register_tool(AgentTool(name=name, definition=tool_definition(name), handler=handler))
    responses = deque([tool_response(name, {"value": "one"}, "model-call-1"), text_response()])

    async def create_response(*args, **kwargs):
        return responses.popleft()

    monkeypatch.setattr(ai_chat, "create_chat_response", create_response)

    asyncio.run(ai_chat.ask_ai_with_agent("question", event=FakePrivateEvent()))
    rows = asyncio.run(audit_rows(name))

    assert [row["event_type"] for row in rows] == ["tool_requested", "execution_started", "execution_completed"]
    assert all(row["tool_call_id"] == "model-call-1" for row in rows)


def test_confirmation_execution_links_original_and_server_call_ids(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(confirmation, "DB_PATH", tmp_path / "confirmations.db")
    allow_all(monkeypatch)
    name = "runtime_audit_confirmation_link_test"

    async def handler(arguments, context):
        return {"ok": True, "message": "executed"}

    register_tool(
        AgentTool(
            name=name,
            definition=tool_definition(name),
            handler=handler,
            side_effect="write",
            risk_level="high",
            requires_confirmation=True,
        )
    )

    async def create_response(*args, **kwargs):
        return tool_response(name, {"value": "one"}, "model-confirm-1")

    monkeypatch.setattr(ai_chat, "create_chat_response", create_response)

    async def run():
        await ai_chat.ask_ai_with_agent("question", event=FakePrivateEvent())
        async with aiosqlite.connect(confirmation.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT confirmation_id, confirmation_code FROM agent_tool_confirmations")
            record = await cursor.fetchone()
        assert record is not None
        reply = await ai_chat.handle_tool_confirmation_reply(
            FakePrivateEvent(f"确认 {record['confirmation_code']}")
        )
        return int(record["confirmation_id"]), reply

    confirmation_id, reply = asyncio.run(run())
    groups = grouped_events(asyncio.run(audit_rows(name)))
    linked = [row for group in groups for row in group if row["confirmation_id"] == confirmation_id]

    assert reply == "executed"
    assert any(group[-1]["event_type"] == "confirmation_required" and group[0]["tool_call_id"] == "model-confirm-1" for group in groups)
    assert linked
    assert any(row["tool_call_id"] == f"confirmation:{confirmation_id}" for row in linked)


def test_idempotency_replay_keeps_distinct_tool_call_ids_and_shared_key(monkeypatch) -> None:
    allow_all(monkeypatch)
    name = "runtime_audit_idempotency_link_test"
    calls = 0

    async def handler(arguments, context):
        nonlocal calls
        calls += 1
        return {"ok": True, "value": arguments["value"]}

    register_tool(
        AgentTool(
            name=name,
            definition=tool_definition(name),
            handler=handler,
            side_effect="write",
            risk_level="medium",
            idempotency_enabled=True,
        )
    )
    responses = deque(
        [
            tool_response(name, {"value": "same"}, "model-idem-1"),
            text_response("first"),
            tool_response(name, {"value": "same"}, "model-idem-2"),
            text_response("second"),
        ]
    )

    async def create_response(*args, **kwargs):
        return responses.popleft()

    monkeypatch.setattr(ai_chat, "create_chat_response", create_response)

    asyncio.run(ai_chat.ask_ai_with_agent("first", event=FakePrivateEvent()))
    asyncio.run(ai_chat.ask_ai_with_agent("second", event=FakePrivateEvent()))
    groups = grouped_events(asyncio.run(audit_rows(name)))
    claimed = next(row for group in groups for row in group if row["event_type"] == "idempotency_claimed")
    replayed = next(row for group in groups for row in group if row["event_type"] == "idempotency_replayed")

    assert calls == 1
    assert claimed["tool_call_id"] == "model-idem-1"
    assert replayed["tool_call_id"] == "model-idem-2"
    assert claimed["idempotency_key"] == replayed["idempotency_key"]
