from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import aiosqlite
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent


DB_PATH = Path("data/message_archive.db")


async def create_message_insights_table(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS message_insights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_archive_id INTEGER NOT NULL,
            group_id TEXT NOT NULL,
            insight_type TEXT NOT NULL,
            segment_index INTEGER NOT NULL DEFAULT 0,
            insight_key TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            content TEXT,
            raw_result TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(message_archive_id, insight_type, insight_key)
        )
        """
    )


async def ensure_message_insights_table(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table' AND name = 'message_insights'
        """
    )
    row = await cursor.fetchone()
    table_sql = str(row[0] or "") if row else ""
    needs_rebuild = bool(row) and (
        "insight_key" not in table_sql
        or "UNIQUE(message_archive_id, insight_type)" in table_sql
    )

    if needs_rebuild:
        await db.execute("ALTER TABLE message_insights RENAME TO message_insights_old")
        await create_message_insights_table(db)
        await db.execute(
            """
            INSERT OR IGNORE INTO message_insights (
                id,
                message_archive_id,
                group_id,
                insight_type,
                segment_index,
                insight_key,
                status,
                content,
                raw_result,
                error,
                created_at,
                updated_at
            )
            SELECT
                id,
                message_archive_id,
                group_id,
                insight_type,
                0,
                '0',
                status,
                content,
                raw_result,
                error,
                created_at,
                updated_at
            FROM message_insights_old
            """
        )
        await db.execute("DROP TABLE message_insights_old")
    else:
        await create_message_insights_table(db)


async def init_archive_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS collected_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT,
                self_id TEXT,
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                sender_name TEXT,
                message_type TEXT NOT NULL,
                sub_type TEXT,
                segment_types TEXT NOT NULL,
                plain_text TEXT,
                raw_message TEXT NOT NULL,
                event_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                received_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_collected_messages_group_created_at
            ON collected_messages (group_id, created_at)
            """
        )
        await db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_collected_messages_group_message_id
            ON collected_messages (group_id, message_id)
            WHERE message_id IS NOT NULL AND message_id != ''
            """
        )
        await ensure_message_insights_table(db)
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_message_insights_group_type
            ON message_insights (group_id, insight_type, status)
            """
        )
        await db.commit()


def event_time_text(event: MessageEvent) -> str:
    event_time = getattr(event, "time", None)
    if isinstance(event_time, int | float):
        return datetime.fromtimestamp(event_time).strftime("%Y-%m-%d %H:%M:%S")
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sender_name(event: GroupMessageEvent) -> str | None:
    sender = getattr(event, "sender", None)
    if sender is None:
        return None
    card = str(getattr(sender, "card", "") or "").strip()
    nickname = str(getattr(sender, "nickname", "") or "").strip()
    return card or nickname or None


def message_segments(event: MessageEvent) -> list[dict[str, object]]:
    segments: list[dict[str, object]] = []
    for segment in event.get_message():
        segments.append(
            {
                "type": segment.type,
                "data": dict(segment.data),
            }
        )
    return segments


def render_segment(segment: dict[str, object]) -> str:
    segment_type = str(segment.get("type", ""))
    data = segment.get("data")
    if not isinstance(data, dict):
        data = {}

    if segment_type == "text":
        return str(data.get("text", ""))
    if segment_type == "at":
        qq = str(data.get("qq", "")).strip()
        return f"@{qq}" if qq else "[@]"
    if segment_type == "image":
        url = data.get("url") or data.get("file") or data.get("file_id") or ""
        return f"[图片:{url}]"
    if segment_type == "record":
        url = data.get("url") or data.get("file") or data.get("file_id") or ""
        return f"[语音:{url}]"
    if segment_type == "video":
        url = data.get("url") or data.get("file") or data.get("file_id") or ""
        return f"[视频:{url}]"
    if segment_type == "file":
        name = data.get("name") or data.get("file") or data.get("file_id") or ""
        return f"[文件:{name}]"
    if segment_type == "reply":
        message_id = data.get("id") or data.get("message_id") or ""
        return f"[回复:{message_id}]"
    if segment_type == "forward":
        forward_id = data.get("id") or data.get("file") or ""
        return f"[聊天记录:{forward_id}]"
    if segment_type == "json":
        return f"[JSON:{data.get('data', '')}]"
    if segment_type == "xml":
        return f"[XML:{data.get('data', '')}]"
    if segment_type == "face":
        return f"[表情:{data.get('id', '')}]"

    return f"[{segment_type}:{dump_json(data)}]"


def render_plain_text(segments: list[dict[str, object]]) -> str:
    rendered = "".join(render_segment(segment) for segment in segments)
    return " ".join(rendered.split()).strip()


def dump_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def event_payload(event: MessageEvent) -> dict[str, object]:
    if hasattr(event, "model_dump"):
        try:
            return event.model_dump(mode="json")
        except Exception:
            pass
    try:
        return event.dict()
    except Exception:
        return {"event": str(event)}


async def insert_collected_message(
    *,
    message_id: str,
    self_id: str,
    group_id: str,
    user_id: str,
    sender_name_value: str | None,
    message_type: str,
    sub_type: str,
    segment_types: list[str],
    plain_text: str,
    raw_message: object,
    event_json: object,
    created_at: str,
) -> None:
    await init_archive_db()
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO collected_messages (
                message_id,
                self_id,
                group_id,
                user_id,
                sender_name,
                message_type,
                sub_type,
                segment_types,
                plain_text,
                raw_message,
                event_json,
                created_at,
                received_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                self_id,
                group_id,
                user_id,
                sender_name_value,
                message_type,
                sub_type,
                dump_json(segment_types),
                plain_text,
                dump_json(raw_message),
                dump_json(event_json),
                created_at,
                now_text,
            ),
        )
        await db.commit()


async def save_group_message(event: GroupMessageEvent) -> None:
    segments = message_segments(event)
    segment_types = [str(segment["type"]) for segment in segments]
    message_id = str(getattr(event, "message_id", "") or "")

    await insert_collected_message(
        message_id=message_id,
        self_id=str(getattr(event, "self_id", "") or ""),
        group_id=str(event.group_id),
        user_id=str(event.user_id),
        sender_name_value=sender_name(event),
        message_type=str(getattr(event, "message_type", "") or "group"),
        sub_type=str(getattr(event, "sub_type", "") or ""),
        segment_types=segment_types,
        plain_text=render_plain_text(segments),
        raw_message=segments,
        event_json=event_payload(event),
        created_at=event_time_text(event),
    )


async def save_ai_reply(
    *,
    group_id: int | str,
    bot_user_id: int | str,
    answer: str,
    source_message_id: int | str | None,
    source_user_id: int | str,
) -> None:
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    message_id = f"ai_reply:{source_message_id or now_text}:{bot_user_id}"
    raw_message = [{"type": "text", "data": {"text": answer}}]
    event_json = {
        "synthetic": True,
        "source": "ai_chat",
        "source_message_id": source_message_id,
        "source_user_id": str(source_user_id),
    }

    await insert_collected_message(
        message_id=message_id,
        self_id=str(bot_user_id),
        group_id=str(group_id),
        user_id=str(bot_user_id),
        sender_name_value="猎bot",
        message_type="group",
        sub_type="ai_reply",
        segment_types=["text"],
        plain_text=answer,
        raw_message=raw_message,
        event_json=event_json,
        created_at=now_text,
    )
