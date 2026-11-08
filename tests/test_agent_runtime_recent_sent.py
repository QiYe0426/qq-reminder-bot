from __future__ import annotations

import asyncio

import pytest

from plugins.agent_runtime import store


@pytest.fixture(autouse=True)
def isolate_runtime_db(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "agent_runs.db")
    monkeypatch.setenv("AGENT_RUN_TRACE_ENABLED", "1")


def test_recent_sent_texts_returns_latest_first() -> None:
    async def scenario() -> list[str]:
        await store.insert_run("run_a", chat_key="group_1")
        await store.append_step("run_a", kind="sent", result="第一句")
        await store.append_step("run_a", kind="sent", result="第二句")
        await store.insert_run("run_b", chat_key="group_2")
        await store.append_step("run_b", kind="sent", result="别的群")
        return await store.recent_sent_texts("group_1", limit=3)

    texts = asyncio.run(scenario())
    assert texts == ["第二句", "第一句"]