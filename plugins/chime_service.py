from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import aiosqlite


DB_PATH = Path("data/reminders.db")
CHIME_MODE_HOURLY = "hourly"
CHIME_MODE_TWICE_DAILY = "twice_daily"
CHIME_MODE_CHOICES = {CHIME_MODE_HOURLY, CHIME_MODE_TWICE_DAILY}
CHIME_MODE_LABELS = {
    CHIME_MODE_HOURLY: "每小时58分",
    CHIME_MODE_TWICE_DAILY: "每天凌晨1:58和下午1:58",
}
TWICE_DAILY_CHIME_HOURS = {1, 13}

_db_ready = False


@dataclass(frozen=True)
class ChimeTarget:
    target_type: str
    target_id: str


async def init_chime_db() -> None:
    global _db_ready
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS hourly_chime_settings (
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                mode TEXT NOT NULL DEFAULT 'hourly',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (target_type, target_id)
            )
            """
        )
        cursor = await db.execute("PRAGMA table_info(hourly_chime_settings)")
        columns = await cursor.fetchall()
        if not any(column[1] == "mode" for column in columns):
            await db.execute("ALTER TABLE hourly_chime_settings ADD COLUMN mode TEXT NOT NULL DEFAULT 'hourly'")
        await db.commit()
    _db_ready = True


async def ensure_chime_db() -> None:
    if not _db_ready:
        await init_chime_db()


def normalize_chime_mode(mode: str | None) -> str:
    return mode if mode in CHIME_MODE_CHOICES else CHIME_MODE_HOURLY


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_chime_mode(text: str) -> str | None:
    raw_mode = text
    for prefix in ("启用🎒常数报时", "开启🎒常数报时"):
        if raw_mode.startswith(prefix):
            raw_mode = raw_mode.removeprefix(prefix)
            break
    raw_mode = raw_mode.strip()
    normalized = re.sub(r"\s+", "", raw_mode).lower()
    if not normalized:
        return CHIME_MODE_HOURLY

    hourly_aliases = {
        "每小时",
        "小时",
        "每小时58分",
        "小时58分",
        "hourly",
    }
    twice_daily_aliases = {
        "每天两次",
        "每日两次",
        "一天两次",
        "两次",
        "双次",
        "定点",
        "1:58和13:58",
        "13:58和1:58",
        "凌晨1:58和下午1:58",
        "下午1:58和凌晨1:58",
        "twicedaily",
        "twice_daily",
    }
    if normalized in hourly_aliases:
        return CHIME_MODE_HOURLY
    if normalized in twice_daily_aliases:
        return CHIME_MODE_TWICE_DAILY
    return None


def should_send_chime(mode: str, now: datetime) -> bool:
    if mode == CHIME_MODE_TWICE_DAILY:
        return now.hour in TWICE_DAILY_CHIME_HOURS
    return mode == CHIME_MODE_HOURLY


def format_chime_time(now: datetime) -> str:
    hour = now.hour
    if hour < 6:
        period = "凌晨"
    elif hour < 12:
        period = "上午"
    elif hour < 18:
        period = "下午"
    else:
        period = "晚上"

    hour_12 = hour % 12
    if hour_12 == 0:
        hour_12 = 12
    return f"{period}{hour_12}:58"


def chime_reply(enabled: bool, mode: str) -> str:
    if enabled:
        return f"已启用🎒常数报时：{CHIME_MODE_LABELS.get(mode, mode)}，请静候佳音。"
    return "已关闭🎒常数报时，感恩遇见！"


async def set_chime_state(
    target_type: str,
    target_id: str,
    enabled: bool,
    mode: str = CHIME_MODE_HOURLY,
) -> dict[str, object]:
    await ensure_chime_db()
    selected_mode = normalize_chime_mode(mode)
    timestamp = now_text()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO hourly_chime_settings (target_type, target_id, enabled, mode, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(target_type, target_id) DO UPDATE SET
                enabled = excluded.enabled,
                mode = excluded.mode,
                updated_at = excluded.updated_at
            """,
            (str(target_type), str(target_id), 1 if enabled else 0, selected_mode, timestamp),
        )
        await db.commit()
    return {
        "ok": True,
        "target_type": str(target_type),
        "target_id": str(target_id),
        "enabled": bool(enabled),
        "mode": selected_mode,
        "mode_label": CHIME_MODE_LABELS.get(selected_mode, selected_mode),
        "updated_at": timestamp,
        "message": chime_reply(bool(enabled), selected_mode),
    }


async def get_chime_state(target_type: str, target_id: str) -> dict[str, object]:
    await ensure_chime_db()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT enabled, mode, updated_at
            FROM hourly_chime_settings
            WHERE target_type = ? AND target_id = ?
            """,
            (str(target_type), str(target_id)),
        )
        row = await cursor.fetchone()
    if row is None:
        return {
            "enabled": False,
            "mode": CHIME_MODE_HOURLY,
            "mode_label": CHIME_MODE_LABELS[CHIME_MODE_HOURLY],
            "updated_at": "",
        }
    mode = normalize_chime_mode(str(row[1] or CHIME_MODE_HOURLY))
    return {
        "enabled": bool(row[0]),
        "mode": mode,
        "mode_label": CHIME_MODE_LABELS.get(mode, mode),
        "updated_at": str(row[2] or ""),
    }


async def group_chime_status_text(group_id: str | int) -> str:
    state = await get_chime_state("group", str(group_id))
    if not state["enabled"]:
        return "关闭"
    return str(state.get("mode_label") or state.get("mode") or "")


async def list_chime_targets() -> list[dict[str, object]]:
    await ensure_chime_db()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT target_type, target_id, mode, updated_at
            FROM hourly_chime_settings
            WHERE enabled = 1
            ORDER BY target_type ASC, target_id ASC
            """
        )
        rows = await cursor.fetchall()
    return [
        {
            "target_type": str(row["target_type"] or ""),
            "target_id": str(row["target_id"] or ""),
            "mode": normalize_chime_mode(str(row["mode"] or CHIME_MODE_HOURLY)),
            "updated_at": str(row["updated_at"] or ""),
        }
        for row in rows
    ]


async def list_chime_group_ids() -> list[str]:
    await ensure_chime_db()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT DISTINCT target_id
            FROM hourly_chime_settings
            WHERE target_type = 'group'
            ORDER BY target_id ASC
            """
        )
        rows = await cursor.fetchall()
    return [str(row[0]) for row in rows if row[0]]
