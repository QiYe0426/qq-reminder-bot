from __future__ import annotations

import asyncio

import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from plugins.agent_runtime import router, tier


@pytest.fixture(autouse=True)
def isolate_dbs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(tier, "DB_PATH", tmp_path / "bot_settings.db")
    monkeypatch.setenv("AGENT_RUNTIME_ROUTING", "0")
    monkeypatch.setenv("AGENT_RUNTIME_GROUP_IDS", "")
    monkeypatch.setenv("AI_V3_GROUP_IDS", "")


def test_default_tier_is_disabled() -> None:
    config = asyncio.run(tier.get_tier_config("700021408"))
    assert config["enabled"] is False
    assert config["probability"] == 5.0
    assert config["cooldown_minutes"] == 10


def test_save_and_normalize_tier() -> None:
    async def scenario() -> dict[str, object]:
        saved = await tier.save_tier_config(
            "700021408",
            {
                "enabled": True,
                "keywords": "猎宝， 小鲸鱼 ,,",
                "probability": 200,
                "cooldown_minutes": -3,
                "daily_limit": 99999,
            },
        )
        loaded = await tier.get_tier_config("700021408")
        assert loaded == saved
        return loaded

    config = asyncio.run(scenario())
    assert config["keywords"] == "猎宝,小鲸鱼"
    assert config["probability"] == 100.0
    assert config["cooldown_minutes"] == 0
    assert config["daily_limit"] == 10000


def test_decide_trigger_matrix() -> None:
    async def scenario() -> None:
        await tier.save_tier_config(
            "123",
            {"enabled": False, "keywords": "猎宝", "probability": 100, "cooldown_minutes": 0, "daily_limit": 0},
        )
        ok, reason = await tier.decide_trigger(
            group_id="123", text="随便聊聊", mentioned=False, run_count_today=0, last_run_at=None
        )
        assert (ok, reason) == (False, "tier_disabled")

        ok, reason = await tier.decide_trigger(
            group_id="123", text="随便", mentioned=True, run_count_today=0, last_run_at=None
        )
        assert (ok, reason) == (True, "mention")

        await tier.save_tier_config(
            "123",
            {"enabled": True, "keywords": "猎宝", "probability": 0, "cooldown_minutes": 0, "daily_limit": 0},
        )
        ok, reason = await tier.decide_trigger(
            group_id="123", text="今天天气不错", mentioned=False, run_count_today=0, last_run_at=None
        )
        assert (ok, reason) == (False, "probability_zero")

        ok, reason = await tier.decide_trigger(
            group_id="123", text="猎宝在吗", mentioned=False, run_count_today=0, last_run_at=None
        )
        assert (ok, reason) == (True, "keyword")

        await tier.save_tier_config(
            "123",
            {"enabled": True, "probability": 50, "cooldown_minutes": 10, "daily_limit": 5},
        )
        ok, reason = await tier.decide_trigger(
            group_id="123",
            text="随便",
            mentioned=False,
            run_count_today=5,
            last_run_at=None,
            rng=lambda: 0.0,
        )
        assert (ok, reason) == (False, "daily_limit")

        ok, reason = await tier.decide_trigger(
            group_id="123",
            text="随便",
            mentioned=False,
            run_count_today=1,
            last_run_at=1000,
            now=1000 + 60,
            rng=lambda: 0.0,
        )
        assert (ok, reason) == (False, "cooldown")

        ok, reason = await tier.decide_trigger(
            group_id="123",
            text="随便",
            mentioned=False,
            run_count_today=1,
            last_run_at=1000,
            now=1000 + 3600,
            rng=lambda: 0.0,
        )
        assert (ok, reason) == (True, "probability")

        ok, reason = await tier.decide_trigger(
            group_id="123",
            text="随便",
            mentioned=False,
            run_count_today=1,
            last_run_at=1000,
            now=1000 + 3600,
            rng=lambda: 0.99,
        )
        assert (ok, reason) == (False, "not_rolled")

    asyncio.run(scenario())


def test_runtime_flags_matrix(monkeypatch) -> None:
    class FakeEvent:
        group_id = 700021408

    assert router.runtime_active_for(FakeEvent()) is False

    monkeypatch.setenv("AGENT_RUNTIME_ROUTING", "1")
    assert router.runtime_active_for(FakeEvent()) is False  # 白名单为空

    monkeypatch.setenv("AGENT_RUNTIME_GROUP_IDS", "700021408")
    assert router.runtime_active_for(FakeEvent()) is True
    assert router.reactions_routed_for(FakeEvent()) is True

    monkeypatch.setenv("GROUP_REACTIONS_ROUTED_BY_RUNTIME", "0")
    assert router.reactions_routed_for(FakeEvent()) is False

    monkeypatch.delenv("AGENT_RUNTIME_GROUP_IDS")
    monkeypatch.setenv("AI_V3_GROUP_IDS", "700021408")
    assert router.runtime_active_for(FakeEvent()) is True


def test_deterministic_reactions_short_circuit(monkeypatch) -> None:
    import plugins.group_reactions as reactions

    class FakeBot:
        self_id = "1467807354"

    class FakeEvent:
        group_id = 700021408

        def get_plaintext(self) -> str:
            return "158"

    monkeypatch.setenv("AGENT_RUNTIME_ROUTING", "1")
    monkeypatch.setenv("AGENT_RUNTIME_GROUP_IDS", "700021408")

    calls = {"keyword": 0, "tease": 0}

    async def fake_keyword(bot, event):
        calls["keyword"] += 1
        return True

    async def fake_tease(bot, event):
        calls["tease"] += 1
        return True

    async def fake_trace(*args, **kwargs):
        return None

    monkeypatch.setattr(reactions, "maybe_keyword_retort", fake_keyword)
    monkeypatch.setattr(reactions, "maybe_bot_tease", fake_tease)
    monkeypatch.setattr(router, "_trace_rule", fake_trace)

    handled = asyncio.run(router.run_deterministic_reactions(FakeBot(), FakeEvent()))

    assert handled is True
    assert calls == {"keyword": 1, "tease": 0}
