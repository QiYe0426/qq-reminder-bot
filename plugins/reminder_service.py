from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite


DB_PATH = Path("data/reminders.db")
TIME_FORMAT = "%Y-%m-%d %H:%M"
TIME_ONLY_FORMAT = "%H:%M"
NUMBER_TEXT = r"\d+|[零〇一二两俩三四五六七八九十百千]+"
RELATIVE_TIME_PATTERN = re.compile(
    rf"^(?:(?P<days>{NUMBER_TEXT})\s*天\s*)?"
    rf"(?:(?P<hours>{NUMBER_TEXT})\s*(?:小时|个小时)\s*)?"
    rf"(?:(?P<minutes>{NUMBER_TEXT})\s*分钟\s*)?"
    r"后\s*(?P<content>.+)$"
)

_db_ready = False


@dataclass(frozen=True)
class ReminderScope:
    user_id: str
    target_type: str
    group_id: str | None = None


async def init_reminder_db() -> None:
    global _db_ready
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                group_id TEXT,
                target_type TEXT NOT NULL,
                remind_at TEXT NOT NULL,
                content TEXT NOT NULL,
                done INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.commit()
    _db_ready = True


async def ensure_reminder_db() -> None:
    if not _db_ready:
        await init_reminder_db()


def parse_number(text: str | None) -> int:
    if not text:
        return 0
    if text.isdigit():
        return int(text)

    digits = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "俩": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    units = {"十": 10, "百": 100, "千": 1000}
    total = 0
    current = 0

    for char in text:
        if char in digits:
            current = digits[char]
        elif char in units:
            unit = units[char]
            if current == 0:
                current = 1
            total += current * unit
            current = 0
        else:
            return 0

    return total + current


def parse_reminder(text: str, *, now: datetime | None = None) -> tuple[datetime, str] | None:
    text = text.strip()
    current_time = now or datetime.now()

    match = RELATIVE_TIME_PATTERN.match(text)
    if match:
        days = parse_number(match.group("days"))
        hours = parse_number(match.group("hours"))
        minutes = parse_number(match.group("minutes"))
        content = match.group("content").strip()
        if content and (days or hours or minutes):
            remind_at = current_time + timedelta(
                days=days,
                hours=hours,
                minutes=minutes,
            )
            return remind_at, content

    parts = text.split(maxsplit=2)

    if len(parts) == 3:
        raw_time = f"{parts[0]} {parts[1]}"
        content = parts[2].strip()
        if content:
            try:
                remind_at = datetime.strptime(raw_time, TIME_FORMAT)
            except ValueError:
                pass
            else:
                return remind_at, content

    time_parts = text.split(maxsplit=1)
    if len(time_parts) != 2:
        return None

    raw_time, content = time_parts[0], time_parts[1].strip()
    if not content:
        return None

    try:
        parsed_time = datetime.strptime(raw_time, TIME_ONLY_FORMAT).time()
    except ValueError:
        return None

    remind_at = current_time.replace(
        hour=parsed_time.hour,
        minute=parsed_time.minute,
        second=0,
        microsecond=0,
    )
    if remind_at <= current_time:
        remind_at += timedelta(days=1)

    return remind_at, content


def normalize_scope(scope: ReminderScope) -> ReminderScope:
    target_type = "group" if scope.target_type == "group" and scope.group_id else "private"
    group_id = scope.group_id if target_type == "group" else None
    return ReminderScope(user_id=str(scope.user_id), target_type=target_type, group_id=group_id)


async def create_reminder(scope: ReminderScope, raw_text: str) -> dict[str, object]:
    await ensure_reminder_db()
    parsed = parse_reminder(raw_text)
    if parsed is None:
        return {
            "ok": False,
            "error": "invalid_format",
            "message": "格式：提醒 2026-06-20 09:00 喝水 或 提醒 09:00 喝水 或 提醒 10分钟后喝水",
        }

    remind_at, content = parsed
    if remind_at <= datetime.now():
        return {"ok": False, "error": "past_time", "message": "提醒时间需要晚于现在。"}

    normalized = normalize_scope(scope)
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO reminders
                (user_id, group_id, target_type, remind_at, content, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                normalized.user_id,
                normalized.group_id,
                normalized.target_type,
                remind_at.strftime(TIME_FORMAT),
                content,
                created_at,
            ),
        )
        await db.commit()
        reminder_id = int(cursor.lastrowid or 0)

    return {
        "ok": True,
        "id": reminder_id,
        "remind_at": remind_at.strftime(TIME_FORMAT),
        "content": content,
        "target_type": normalized.target_type,
        "group_id": normalized.group_id or "",
        "message": f"已创建提醒 #{reminder_id}：{remind_at.strftime(TIME_FORMAT)} {content}",
    }


async def list_reminders(user_id: str, *, limit: int = 10) -> list[dict[str, object]]:
    await ensure_reminder_db()
    safe_limit = max(1, min(int(limit or 10), 50))
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, remind_at, content, target_type, group_id
            FROM reminders
            WHERE done = 0 AND user_id = ?
            ORDER BY remind_at ASC
            LIMIT ?
            """,
            (str(user_id), safe_limit),
        )
        rows = await cursor.fetchall()

    return [
        {
            "id": int(row["id"] or 0),
            "remind_at": str(row["remind_at"] or ""),
            "content": str(row["content"] or ""),
            "target_type": str(row["target_type"] or ""),
            "group_id": str(row["group_id"] or ""),
        }
        for row in rows
    ]


def format_reminder_list(reminders: list[dict[str, object]]) -> str:
    if not reminders:
        return "你现在没有未完成提醒。"

    lines = ["未完成提醒："]
    for item in reminders:
        lines.append(f"#{item['id']} {item['remind_at']} {item['content']}")
    return "\n".join(lines)


async def list_reminders_result(user_id: str, *, limit: int = 10) -> dict[str, object]:
    reminders = await list_reminders(user_id, limit=limit)
    return {"ok": True, "reminders": reminders, "message": format_reminder_list(reminders)}


async def cancel_reminder(user_id: str, reminder_id: int) -> dict[str, object]:
    await ensure_reminder_db()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE reminders
            SET done = 1
            WHERE id = ? AND user_id = ? AND done = 0
            """,
            (int(reminder_id), str(user_id)),
        )
        await db.commit()
        changed = cursor.rowcount

    if changed:
        return {"ok": True, "id": int(reminder_id), "message": f"已取消提醒 #{int(reminder_id)}。"}
    return {
        "ok": False,
        "id": int(reminder_id),
        "error": "not_found",
        "message": "没有找到这个未完成提醒，或它不属于你。",
    }


async def due_reminders(*, now: datetime | None = None) -> list[dict[str, object]]:
    await ensure_reminder_db()
    now_text = (now or datetime.now()).strftime(TIME_FORMAT)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, user_id, group_id, target_type, content
            FROM reminders
            WHERE done = 0 AND remind_at <= ?
            ORDER BY remind_at ASC
            """,
            (now_text,),
        )
        rows = await cursor.fetchall()

    return [
        {
            "id": int(row["id"] or 0),
            "user_id": str(row["user_id"] or ""),
            "group_id": str(row["group_id"] or ""),
            "target_type": str(row["target_type"] or ""),
            "content": str(row["content"] or ""),
        }
        for row in rows
    ]


async def mark_reminders_done(reminder_ids: list[int]) -> None:
    if not reminder_ids:
        return
    await ensure_reminder_db()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "UPDATE reminders SET done = 1 WHERE id = ?",
            [(int(item_id),) for item_id in reminder_ids],
        )
        await db.commit()
