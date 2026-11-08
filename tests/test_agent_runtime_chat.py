from __future__ import annotations

import asyncio
from types import SimpleNamespace

import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from plugins.agent_runtime import chat as chat_module
from plugins.agent_runtime import store


@pytest.fixture(autouse=True)
def isolate_runtime_db(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "agent_runs.db")
    monkeypatch.setenv("AGENT_RUN_TRACE_ENABLED", "1")
    monkeypatch.setenv("AI_CHAT_ENGINE", "legacy")
    monkeypatch.setenv("AI_V3_GROUP_IDS", "")


def _response(content: str, prompt_tokens: int = 10, completion_tokens: int = 5, reasoning: str = ""):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, reasoning_content=reasoning))],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            prompt_tokens_details=None,
            completion_tokens_details=None,
        ),
    )


def _event(group_id: int = 700021408, user_id: int = 42):
    return SimpleNamespace(group_id=group_id, user_id=user_id)


def test_should_use_v3_env_matrix(monkeypatch) -> None:
    event = _event()
    assert chat_module.should_use_v3(event) is False  # 默认 legacy

    monkeypatch.setenv("AI_CHAT_ENGINE", "v3")
    assert chat_module.should_use_v3(event) is False  # 白名单为空

    monkeypatch.setenv("AI_V3_GROUP_IDS", "700021408, 123")
    assert chat_module.should_use_v3(event) is True
    assert chat_module.should_use_v3(_event(group_id=999)) is False


def test_extract_plan_variants() -> None:
    plan = chat_module.extract_plan('{"reply": "嗯", "need": "web", "queries": ["新闻"]}')
    assert plan == {"reply": "嗯", "need": "web", "queries": ["新闻"]}

    fenced = chat_module.extract_plan('```json\n{"reply": "在", "need": "none"}\n```')
    assert fenced["reply"] == "在"
    assert fenced["need"] == "none"

    plain = chat_module.extract_plan("随便聊聊")
    assert plain == {"reply": "随便聊聊", "need": "none", "queries": []}

    weird = chat_module.extract_plan('{"reply": "x", "need": "nonsense"}')
    assert weird["need"] == "none"


def test_answer_v3_single_call(monkeypatch) -> None:
    async def fake_persona() -> str:
        return "你是猎宝"

    async def fake_chat(messages, temperature=None):
        return _response('{"reply": "你好呀", "need": "none", "queries": []}')

    monkeypatch.setattr(chat_module, "_persona", fake_persona)
    monkeypatch.setattr(chat_module, "_chat", fake_chat)

    answer = asyncio.run(chat_module.answer_v3("在吗", event=_event()))

    assert answer == "你好呀"
    runs = asyncio.run(store.list_runs())
    assert len(runs) == 1
    assert runs[0]["engine"] == "v3"
    assert runs[0]["route"] == "R1"
    assert runs[0]["status"] == "ok"
    detail = asyncio.run(store.get_run(runs[0]["run_id"]))
    assert detail is not None
    assert [step["kind"] for step in detail["steps"]] == ["think", "sent"]
    assert detail["usage_prompt_tokens"] == 10


def test_answer_v3_retrieval_then_second_call(monkeypatch) -> None:
    calls: list[list[dict[str, object]]] = []

    async def fake_persona() -> str:
        return ""

    async def fake_chat(messages, temperature=None):
        calls.append(messages)
        if len(calls) == 1:
            return _response('{"reply": "我查下", "need": "kb", "queries": ["复仇者"]}')
        return _response("复仇者是 STS2 的一张攻击牌。")

    async def fake_retrieve(need, queries, question):
        assert need == "kb"
        return ("复仇者：造成 8 点伤害。", 1)

    monkeypatch.setattr(chat_module, "_persona", fake_persona)
    monkeypatch.setattr(chat_module, "_chat", fake_chat)
    monkeypatch.setattr(chat_module, "_retrieve", fake_retrieve)

    answer = asyncio.run(chat_module.answer_v3("复仇者是啥", event=_event()))

    assert answer == "复仇者是 STS2 的一张攻击牌。"
    assert len(calls) == 2
    runs = asyncio.run(store.list_runs())
    assert runs[0]["route"] == "R2"
    detail = asyncio.run(store.get_run(runs[0]["run_id"]))
    assert detail is not None
    kinds = [step["kind"] for step in detail["steps"]]
    assert "retrieval" in kinds
    assert "sent" in kinds


def test_answer_v3_records_reasoning_separately(monkeypatch) -> None:
    async def fake_persona() -> str:
        return ""

    async def fake_chat(messages, temperature=None):
        return _response(
            '{"reply": "在的", "need": "none", "queries": []}',
            reasoning="用户只是打招呼，回一句就行。",
        )

    monkeypatch.setattr(chat_module, "_persona", fake_persona)
    monkeypatch.setattr(chat_module, "_chat", fake_chat)

    answer = asyncio.run(chat_module.answer_v3("在吗", event=_event()))

    assert answer == "在的"
    runs = asyncio.run(store.list_runs())
    detail = asyncio.run(store.get_run(runs[0]["run_id"]))
    kinds = [step["kind"] for step in detail["steps"]]
    assert "reasoning" in kinds
    reasoning_step = next(step for step in detail["steps"] if step["kind"] == "reasoning")
    assert "打招呼" in reasoning_step["result"]
    sent_step = next(step for step in detail["steps"] if step["kind"] == "sent")
    assert sent_step["result"] == "在的"


def test_answer_v3_marks_error(monkeypatch) -> None:
    async def fake_persona() -> str:
        return ""

    async def boom(messages, temperature=None):
        raise RuntimeError("model down")

    monkeypatch.setattr(chat_module, "_persona", fake_persona)
    monkeypatch.setattr(chat_module, "_chat", boom)

    with pytest.raises(RuntimeError):
        asyncio.run(chat_module.answer_v3("在吗", event=_event()))

    runs = asyncio.run(store.list_runs())
    assert runs[0]["status"] == "error"
