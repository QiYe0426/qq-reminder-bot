from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from plugins import ai_chat

from plugins.agent_runtime import recorder as recorder_module
from plugins.agent_runtime import store


@pytest.fixture(autouse=True)
def isolate_runtime_db(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "agent_runs.db")
    monkeypatch.setenv("AGENT_RUN_TRACE_ENABLED", "1")


def test_chat_key_for() -> None:
    assert recorder_module.chat_key_for("group", 123) == "group_123"
    assert recorder_module.chat_key_for("private", "456") == "private_456"
    assert recorder_module.chat_key_for("", "") == "unknown_unknown"


def test_store_roundtrip() -> None:
    async def scenario() -> None:
        await store.insert_run(
            "run_1",
            chat_key="group_1",
            group_id="1",
            kind="group",
            engine="legacy",
            trigger_summary="hi",
            trigger={"a": 1},
            model="m",
        )
        await store.update_run("run_1", system_prompt="sys", user_prompt="usr")
        await store.append_step("run_1", kind="think", result="thinking")
        await store.append_step(
            "run_1",
            kind="tool_call",
            tool_name="web_search",
            args={"q": "x"},
            result={"ok": 1},
            ok=True,
            duration_ms=12,
        )
        runs = await store.list_runs()
        assert len(runs) == 1
        assert runs[0]["step_count"] == 2
        detail = await store.get_run("run_1")
        assert detail is not None
        assert detail["system_prompt"] == "sys"
        assert detail["trigger"] == {"a": 1}
        assert [step["kind"] for step in detail["steps"]] == ["think", "tool_call"]
        assert detail["steps"][1]["args"] == {"q": "x"}
        assert detail["steps"][1]["ok"] is True

    asyncio.run(scenario())


def test_update_run_rejects_unknown_field() -> None:
    async def scenario() -> None:
        await store.insert_run("run_2", chat_key="c")
        with pytest.raises(ValueError):
            await store.update_run("run_2", chat_key="x")

    asyncio.run(scenario())


def test_recorder_lifecycle() -> None:
    async def scenario() -> None:
        recorder = await recorder_module.start_run(
            chat_key="group_1",
            kind="group",
            trigger_summary="hi",
            model="m",
        )
        assert recorder.enabled is True
        await recorder.set_prompts(system_prompt="S", user_prompt="U")
        await recorder.record_llm_turn(
            usage={"prompt_tokens": 10, "completion_tokens": 5},
            content="thinking",
        )
        await recorder.record_tool(
            tool_name="web_search",
            args={"q": "x"},
            result={"ok": True},
            ok=True,
            duration_ms=3,
        )
        await recorder.record_sent("hello", meta={"via": "respond"})
        await recorder.finish(status="ok", finish_reason="answered")

        detail = await store.get_run(recorder.run_id)
        assert detail is not None
        assert detail["status"] == "ok"
        assert detail["finish_reason"] == "answered"
        assert detail["usage_prompt_tokens"] == 10
        assert detail["usage_completion_tokens"] == 5
        assert [step["kind"] for step in detail["steps"]] == ["think", "tool_call", "sent"]

    asyncio.run(scenario())


def test_recorder_disabled_when_env_off(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_RUN_TRACE_ENABLED", "0")

    async def scenario() -> None:
        recorder = await recorder_module.start_run(chat_key="group_1")
        assert recorder.enabled is False
        await recorder.set_prompts(system_prompt="S")
        await recorder.record_sent("hello")
        await recorder.finish()
        assert await store.list_runs() == []

    asyncio.run(scenario())


def test_recorder_never_raises_when_store_breaks(monkeypatch) -> None:
    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "insert_run", boom)

    async def scenario() -> None:
        recorder = await recorder_module.start_run(chat_key="group_1")
        assert recorder.enabled is False
        await recorder.set_prompts(system_prompt="S")
        await recorder.record_sent("still fine")
        await recorder.finish()

    asyncio.run(scenario())


def test_prune_removes_old_runs() -> None:
    async def scenario() -> None:
        now = int(time.time())
        await store.insert_run("old", chat_key="c", started_at=now - 40 * 86400)
        await store.append_step("old", kind="think", result="x")
        await store.insert_run("new", chat_key="c", started_at=now)
        deleted = await store.prune_runs(retention_days=14)
        assert deleted == 1
        assert [row["run_id"] for row in await store.list_runs()] == ["new"]
        assert await store.get_run("old") is None

    asyncio.run(scenario())


def test_runtime_stats() -> None:
    async def scenario() -> None:
        await store.insert_run("a", chat_key="c")
        await store.insert_run("b", chat_key="c")
        await store.update_run("b", status="error")
        stats = await store.runtime_stats()
        assert stats["total"] == 2
        assert stats["errors"] == 1
        assert stats["today"] == 2

    asyncio.run(scenario())


def test_recorder_keeps_reasoning_and_content_apart() -> None:
    async def scenario() -> None:
        recorder = await recorder_module.start_run(chat_key="group_1", engine="v3", route="R1")
        await recorder.record_llm_turn(
            usage={"prompt_tokens": 5, "completion_tokens": 3},
            content="",
            reasoning="先判断用户在打招呼。",
        )
        await recorder.record_llm_turn(usage=None, content="你好呀", meta={"plan": True})
        await recorder.finish(status="ok")
        detail = await store.get_run(recorder.run_id)
        assert detail is not None
        assert [step["kind"] for step in detail["steps"]] == ["reasoning", "think"]

    asyncio.run(scenario())


def test_ask_ai_with_agent_records_run(monkeypatch) -> None:
    async def fake_loop(question, *, extra_context="", event, recorder):
        await recorder.record_llm_turn(
            usage={"prompt_tokens": 3, "completion_tokens": 2},
            content="thinking",
        )
        await recorder.record_sent("hello", meta={"via": "content"})
        return "hello"

    monkeypatch.setattr(ai_chat, "_run_agent_loop", fake_loop)
    event = SimpleNamespace(user_id=42, message_id="m1")

    answer = asyncio.run(ai_chat.ask_ai_with_agent("hi", event=event))

    assert answer == "hello"
    runs = asyncio.run(store.list_runs())
    assert len(runs) == 1
    assert runs[0]["status"] == "ok"
    assert runs[0]["chat_key"] == "private_42"
    detail = asyncio.run(store.get_run(runs[0]["run_id"]))
    assert detail is not None
    assert detail["trigger_summary"] == "hi"
    assert [step["kind"] for step in detail["steps"]] == ["think", "sent"]


def test_ask_ai_with_agent_marks_error_and_reraises(monkeypatch) -> None:
    async def boom(question, *, extra_context="", event, recorder):
        raise RuntimeError("model down")

    monkeypatch.setattr(ai_chat, "_run_agent_loop", boom)
    event = SimpleNamespace(user_id=42, message_id="m1")

    with pytest.raises(RuntimeError):
        asyncio.run(ai_chat.ask_ai_with_agent("hi", event=event))

    runs = asyncio.run(store.list_runs())
    assert len(runs) == 1
    assert runs[0]["status"] == "error"
