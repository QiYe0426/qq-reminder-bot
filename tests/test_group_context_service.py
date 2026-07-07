import asyncio

from plugins import message_archive
from plugins.group_context_service import (
    group_context_result,
    remember_transient_group_message,
)


def test_group_context_reads_archived_messages_with_keyword(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(message_archive, "DB_PATH", tmp_path / "message_archive.db")

    async def run() -> dict[str, object]:
        await message_archive.insert_collected_message(
            message_id="m1",
            self_id="bot",
            group_id="g1",
            user_id="u1",
            sender_name_value="群友A",
            message_type="group",
            sub_type="normal",
            segment_types=["text"],
            plain_text="今天想聊巨像这张牌",
            raw_message=[{"type": "text", "data": {"text": "今天想聊巨像这张牌"}}],
            event_json={},
            created_at="2026-07-07 10:00:00",
        )
        await message_archive.insert_collected_message(
            message_id="m2",
            self_id="bot",
            group_id="g1",
            user_id="u2",
            sender_name_value="群友B",
            message_type="group",
            sub_type="normal",
            segment_types=["text"],
            plain_text="这条消息不相关",
            raw_message=[{"type": "text", "data": {"text": "这条消息不相关"}}],
            event_json={},
            created_at="2026-07-07 10:01:00",
        )
        return await group_context_result(
            group_id="g1",
            collector_enabled=True,
            keyword="巨像",
            limit=10,
        )

    result = asyncio.run(run())

    assert result["source"] == "archive"
    assert result["count"] == 1
    assert result["messages"][0]["sender_name"] == "群友A"
    assert "巨像" in result["context"]


def test_group_context_reads_transient_messages_when_collector_off() -> None:
    group_id = "transient-test-group"
    remember_transient_group_message(
        group_id=group_id,
        message_id="t1",
        user_id="u1",
        sender_name="群友A",
        plain_text="刚才说要吃药",
        segment_types="text",
        created_at="2026-07-07 10:00:00",
    )
    remember_transient_group_message(
        group_id=group_id,
        message_id="t2",
        user_id="u2",
        sender_name="群友B",
        plain_text="另一个话题",
        segment_types="text",
        created_at="2026-07-07 10:01:00",
    )

    result = asyncio.run(
        group_context_result(
            group_id=group_id,
            collector_enabled=False,
            keyword="吃药",
            limit=10,
        )
    )

    assert result["source"] == "transient"
    assert result["count"] == 1
    assert result["messages"][0]["sender_name"] == "群友A"
    assert "吃药" in result["context"]
