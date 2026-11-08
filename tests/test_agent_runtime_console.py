from __future__ import annotations

import asyncio

import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()


@pytest.fixture(autouse=True)
def isolate_runtime_db(tmp_path, monkeypatch) -> None:
    from plugins.agent_runtime import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "agent_runs.db")
    monkeypatch.setenv("AGENT_RUN_TRACE_ENABLED", "1")


def test_qqa_static_assets_exist() -> None:
    from plugins import admin_console

    qqa_dir = admin_console.STATIC_DIR / "qqa"
    for name in ("index.html", "app.js", "extra.css", "style.css"):
        assert (qqa_dir / name).is_file(), f"缺少前端资源 {name}"

    page = (qqa_dir / "index.html").read_text(encoding="utf-8")
    for tab in ("会话", "存档", "群管理", "画像", "知识库", "用量", "设置", "连接"):
        assert tab in page, f"页面缺少页签 {tab}"

    script = (qqa_dir / "app.js").read_text(encoding="utf-8")
    for endpoint in ("/api/groups", "/api/knowledge", "companions", "/archive"):
        assert endpoint in script, f"前端没有接上旧接口 {endpoint}"


def test_qqa_overview_shape() -> None:
    from plugins import admin_console
    from plugins.agent_runtime import store

    async def scenario() -> dict[str, object]:
        await store.insert_run("run_a", chat_key="group_1", kind="group")
        await store.update_run(
            "run_a",
            usage_prompt_tokens=10,
            usage_completion_tokens=5,
            status="ok",
        )
        return await admin_console.qqa_overview()

    overview = asyncio.run(scenario())

    assert str(overview["version"]).startswith("v")
    assert overview["qqa_prefix"].endswith("/qqa")
    assert set(overview["bot"]) == {"connected", "self_id"}
    today = overview["today"]
    assert today["runs"] == 1
    assert today["prompt_tokens"] == 10
    assert today["completion_tokens"] == 5
    assert overview["stats"]["total"] == 1


def test_qqa_overview_reports_models(monkeypatch) -> None:
    from plugins import admin_console

    monkeypatch.setenv("AI_MODEL", "chat-model")
    monkeypatch.setenv("AI_AGENT_MODEL", "agent-model")

    models = admin_console.qqa_models()

    assert models["chat"] == "chat-model"
    assert models["agent"] == "agent-model"


def test_today_totals_counts_errors() -> None:
    from plugins.agent_runtime import store

    async def scenario() -> dict[str, object]:
        await store.insert_run("ok_run", chat_key="group_1")
        await store.update_run("ok_run", status="ok")
        await store.insert_run("bad_run", chat_key="group_1")
        await store.update_run("bad_run", status="error")
        return await store.today_totals()

    totals = asyncio.run(scenario())

    assert totals["runs"] == 2
    assert totals["errors"] == 1
