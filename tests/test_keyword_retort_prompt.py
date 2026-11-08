from __future__ import annotations

import asyncio

import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from plugins import group_reactions as gr


def test_normalize_rule_keeps_prompt() -> None:
    rule = gr.normalize_keyword_retort_rule(
        {"keyword": "158", "prompt": "毒舌一点，20 字以内", "replies": []}
    )
    assert rule is not None
    assert rule["prompt"] == "毒舌一点，20 字以内"


def test_prompt_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(gr, "KEYWORD_RETORT_DB_PATH", tmp_path / "keyword_retorts.db")

    async def scenario() -> dict[str, object]:
        await gr.save_keyword_retort_rules(
            "700021408",
            [
                {
                    "keyword": "158",
                    "enabled": True,
                    "prompt": "用一句俏皮话吐槽",
                    "replies": [{"reply_type": "text", "content": "预设回复", "enabled": True}],
                }
            ],
        )
        rules = await gr.list_keyword_retort_rules("700021408")
        return next(rule for rule in rules if rule["keyword"] == "158")

    rule = asyncio.run(scenario())
    assert rule["prompt"] == "用一句俏皮话吐槽"
    assert rule["replies"][0]["content"] == "预设回复"


def test_prompt_rule_uses_ai_generation(monkeypatch) -> None:
    sent: list[str] = []

    class FakeBot:
        self_id = "1467807354"

        async def send(self, event, message) -> None:  # noqa: ANN001
            sent.append(str(message))

    class FakeEvent:
        group_id = 700021408
        user_id = 42

        def get_plaintext(self) -> str:
            return "158"

    async def fake_enabled(event) -> bool:  # noqa: ANN001
        return True

    async def fake_rule(group_id, event):  # noqa: ANN001
        return {"id": 1, "keyword": "158", "enabled": True, "prompt": "毒舌一句", "replies": []}

    async def fake_consume(group_id, rule_id) -> bool:  # noqa: ANN001
        return True

    async def fake_generate(prompt_text, event, rule) -> str:  # noqa: ANN001
        return "AI 生成的回怼"

    async def fail_send_rule(bot, event, rule) -> bool:  # noqa: ANN001
        raise AssertionError("有提示词时不应走预设回复")

    monkeypatch.setattr(gr, "keyword_retort_enabled", fake_enabled)
    monkeypatch.setattr(gr, "matching_keyword_retort_rule", fake_rule)
    monkeypatch.setattr(gr, "consume_keyword_retort_rule_usage", fake_consume)
    monkeypatch.setattr(gr, "generate_ai_retort", fake_generate)
    monkeypatch.setattr(gr, "send_keyword_retort", fail_send_rule)

    handled = asyncio.run(gr.maybe_keyword_retort(FakeBot(), FakeEvent()))

    assert handled is True
    assert sent == ["AI 生成的回怼"]


def test_empty_prompt_falls_back_to_preset(monkeypatch) -> None:
    sent: list[str] = []

    class FakeBot:
        self_id = "1467807354"

        async def send(self, event, message) -> None:  # noqa: ANN001
            sent.append(str(message))

    class FakeEvent:
        group_id = 700021408
        user_id = 42

        def get_plaintext(self) -> str:
            return "158"

    async def fake_enabled(event) -> bool:  # noqa: ANN001
        return True

    async def fake_rule(group_id, event):  # noqa: ANN001
        return {"id": 1, "keyword": "158", "enabled": True, "prompt": "", "replies": []}

    async def fake_consume(group_id, rule_id) -> bool:  # noqa: ANN001
        return True

    async def fake_send_rule(bot, event, rule) -> bool:  # noqa: ANN001
        sent.append("预设图")
        return True

    monkeypatch.setattr(gr, "keyword_retort_enabled", fake_enabled)
    monkeypatch.setattr(gr, "matching_keyword_retort_rule", fake_rule)
    monkeypatch.setattr(gr, "consume_keyword_retort_rule_usage", fake_consume)
    monkeypatch.setattr(gr, "send_keyword_retort", fake_send_rule)

    handled = asyncio.run(gr.maybe_keyword_retort(FakeBot(), FakeEvent()))

    assert handled is True
    assert sent == ["预设图"]