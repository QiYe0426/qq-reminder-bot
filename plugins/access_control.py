from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv
from nonebot.adapters.onebot.v11 import Event, GroupMessageEvent, MessageEvent


load_dotenv(".env.local")

DB_PATH = Path("data/bot_settings.db")
ADMIN_USER_IDS_ENV = "BOT_ADMIN_USER_IDS"
ADMIN_USER_IDS_FALLBACK_ENVS = ("BOT_OWNER_USER_IDS", "OWNER_USER_IDS")

FEATURE_REMINDER = "reminder"
FEATURE_COLLECTOR = "collector"
FEATURE_COMPANION = "companion"
FEATURE_AI_CHAT = "ai_chat"
FEATURE_BOT_TEASE = "bot_tease"
FEATURE_CONSTANT_RETORT = "constant_retort"
FEATURE_DAILY_REPORT_AUTO = "daily_report_auto"

FEATURE_LABELS = {
    FEATURE_AI_CHAT: "AI 对话",
    FEATURE_REMINDER: "提醒",
    FEATURE_COLLECTOR: "消息采集",
    FEATURE_COMPANION: "陪伴画像",
    FEATURE_BOT_TEASE: "调戏其他bot",
    FEATURE_CONSTANT_RETORT: "🎒常数回怼",
}

FEATURE_DEFAULTS = {
    FEATURE_AI_CHAT: True,
    FEATURE_REMINDER: False,
    FEATURE_COLLECTOR: False,
    FEATURE_COMPANION: False,
    FEATURE_BOT_TEASE: False,
    FEATURE_CONSTANT_RETORT: False,
    FEATURE_DAILY_REPORT_AUTO: True,
}

FEATURE_LIMIT_DEFAULTS = {
    FEATURE_BOT_TEASE: {"per_minute": 1, "per_hour": 3, "per_day": 6},
    FEATURE_CONSTANT_RETORT: {"per_minute": 5, "per_hour": 10, "per_day": 15},
}

FEATURE_ALIASES = {
    "ai": FEATURE_AI_CHAT,
    "ai对话": FEATURE_AI_CHAT,
    "ai聊天": FEATURE_AI_CHAT,
    "aichat": FEATURE_AI_CHAT,
    "ai chat": FEATURE_AI_CHAT,
    "AI": FEATURE_AI_CHAT,
    "AI对话": FEATURE_AI_CHAT,
    "AI聊天": FEATURE_AI_CHAT,
    "AI Chat": FEATURE_AI_CHAT,
    "智能对话": FEATURE_AI_CHAT,
    "聊天": FEATURE_AI_CHAT,
    "提醒": FEATURE_REMINDER,
    "定时提醒": FEATURE_REMINDER,
    "采集": FEATURE_COLLECTOR,
    "日报": FEATURE_COLLECTOR,
    "日报采集": FEATURE_COLLECTOR,
    "消息采集": FEATURE_COLLECTOR,
    "群消息采集": FEATURE_COLLECTOR,
    "群聊采集": FEATURE_COLLECTOR,
    "陪伴": FEATURE_COMPANION,
    "陪伴画像": FEATURE_COMPANION,
    "画像": FEATURE_COMPANION,
    "性格画像": FEATURE_COMPANION,
    "情感陪伴": FEATURE_COMPANION,
    "调戏bot": FEATURE_BOT_TEASE,
    "调戏其他bot": FEATURE_BOT_TEASE,
    "逗bot": FEATURE_BOT_TEASE,
    "逗其他bot": FEATURE_BOT_TEASE,
    "bot_tease": FEATURE_BOT_TEASE,
    "常数回怼": FEATURE_CONSTANT_RETORT,
    "🎒常数回怼": FEATURE_CONSTANT_RETORT,
    "158回怼": FEATURE_CONSTANT_RETORT,
    "constant_retort": FEATURE_CONSTANT_RETORT,
}

_db_ready = False


def parse_id_set(raw_value: str) -> set[str]:
    ids: set[str] = set()
    for item in raw_value.replace("，", ",").split(","):
        item = item.strip()
        if item:
            ids.add(item)
    return ids


def admin_user_ids() -> set[str]:
    user_ids = parse_id_set(os.getenv(ADMIN_USER_IDS_ENV, ""))
    for env_name in ADMIN_USER_IDS_FALLBACK_ENVS:
        user_ids.update(parse_id_set(os.getenv(env_name, "")))
    return user_ids


def admin_denial(event: Event) -> str | None:
    allowed_user_ids = admin_user_ids()
    if not allowed_user_ids:
        return f"管理员还没有配置，请在服务器 .env.local 设置 {ADMIN_USER_IDS_ENV}=你的QQ号。"
    if event.get_user_id() in allowed_user_ids:
        return None
    return "这个命令只有管理员可以使用。"


def normalize_feature_name(raw_name: str) -> str | None:
    normalized = raw_name.strip()
    return FEATURE_ALIASES.get(normalized) or FEATURE_ALIASES.get(normalized.lower())


async def init_access_db() -> None:
    global _db_ready
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS group_feature_settings (
                group_id TEXT NOT NULL,
                feature TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (group_id, feature)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS group_feature_limits (
                group_id TEXT NOT NULL,
                feature TEXT NOT NULL,
                per_minute INTEGER NOT NULL DEFAULT 0,
                per_hour INTEGER NOT NULL DEFAULT 0,
                per_day INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (group_id, feature)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS group_feature_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                feature TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_group_feature_usage_group_feature_time
            ON group_feature_usage (group_id, feature, created_at)
            """
        )
        await db.commit()
    _db_ready = True


async def ensure_access_db() -> None:
    if not _db_ready:
        await init_access_db()


async def set_group_feature(group_id: str, feature: str, enabled: bool) -> None:
    await ensure_access_db()
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO group_feature_settings (group_id, feature, enabled, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(group_id, feature) DO UPDATE SET
                enabled = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (group_id, feature, 1 if enabled else 0, updated_at),
        )
        await db.commit()


async def is_group_feature_enabled(group_id: str, feature: str) -> bool:
    await ensure_access_db()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT enabled
            FROM group_feature_settings
            WHERE group_id = ? AND feature = ?
            """,
            (group_id, feature),
        )
        row = await cursor.fetchone()

    if row is None:
        return FEATURE_DEFAULTS.get(feature, False)
    return bool(row[0])



def normalize_feature_limits(feature: str, raw_limits: dict[str, object] | None = None) -> dict[str, int]:
    defaults = FEATURE_LIMIT_DEFAULTS.get(feature, {"per_minute": 0, "per_hour": 0, "per_day": 0})
    raw_limits = raw_limits if isinstance(raw_limits, dict) else {}
    normalized: dict[str, int] = {}
    for key in ("per_minute", "per_hour", "per_day"):
        try:
            value = int(raw_limits.get(key, defaults.get(key, 0)))
        except (TypeError, ValueError):
            value = int(defaults.get(key, 0))
        normalized[key] = min(max(value, 0), 999)
    return normalized


async def get_group_feature_limits(group_id: str | int, feature: str) -> dict[str, int]:
    await ensure_access_db()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT per_minute, per_hour, per_day
            FROM group_feature_limits
            WHERE group_id = ? AND feature = ?
            """,
            (str(group_id), feature),
        )
        row = await cursor.fetchone()
    if row is None:
        return normalize_feature_limits(feature)
    return normalize_feature_limits(
        feature,
        {"per_minute": row[0], "per_hour": row[1], "per_day": row[2]},
    )


async def get_group_feature_usage(group_id: str | int, feature: str) -> dict[str, int]:
    await ensure_access_db()
    now = datetime.now()
    thresholds = {
        "per_minute": (now - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"),
        "per_hour": (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
        "per_day": (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
    }
    usage: dict[str, int] = {}
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            DELETE FROM group_feature_usage
            WHERE group_id = ? AND feature = ? AND created_at < ?
            """,
            (str(group_id), feature, thresholds["per_day"]),
        )
        for key, threshold in thresholds.items():
            cursor = await db.execute(
                """
                SELECT COUNT(*)
                FROM group_feature_usage
                WHERE group_id = ? AND feature = ? AND created_at >= ?
                """,
                (str(group_id), feature, threshold),
            )
            row = await cursor.fetchone()
            usage[key] = int(row[0] or 0) if row else 0
        await db.commit()
    return usage


async def set_group_feature_limits(group_id: str | int, feature: str, limits: dict[str, object]) -> None:
    await ensure_access_db()
    normalized = normalize_feature_limits(feature, limits)
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO group_feature_limits (group_id, feature, per_minute, per_hour, per_day, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_id, feature) DO UPDATE SET
                per_minute = excluded.per_minute,
                per_hour = excluded.per_hour,
                per_day = excluded.per_day,
                updated_at = excluded.updated_at
            """,
            (
                str(group_id),
                feature,
                normalized["per_minute"],
                normalized["per_hour"],
                normalized["per_day"],
                updated_at,
            ),
        )
        await db.commit()


async def consume_group_feature_usage(group_id: str | int, feature: str) -> bool:
    await ensure_access_db()
    limits = await get_group_feature_limits(group_id, feature)
    if any(limits[key] <= 0 for key in ("per_minute", "per_hour", "per_day")):
        return False

    now = datetime.now()
    now_text = now.strftime("%Y-%m-%d %H:%M:%S")
    minute_start = (now - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    hour_start = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    day_start = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    group_text = str(group_id)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            DELETE FROM group_feature_usage
            WHERE group_id = ? AND feature = ? AND created_at < ?
            """,
            (group_text, feature, day_start),
        )
        counts: dict[str, int] = {}
        for key, threshold in (
            ("per_minute", minute_start),
            ("per_hour", hour_start),
            ("per_day", day_start),
        ):
            cursor = await db.execute(
                """
                SELECT COUNT(*)
                FROM group_feature_usage
                WHERE group_id = ? AND feature = ? AND created_at >= ?
                """,
                (group_text, feature, threshold),
            )
            row = await cursor.fetchone()
            counts[key] = int(row[0] or 0) if row else 0
        if any(counts[key] >= limits[key] for key in ("per_minute", "per_hour", "per_day")):
            await db.commit()
            return False
        await db.execute(
            """
            INSERT INTO group_feature_usage (group_id, feature, created_at)
            VALUES (?, ?, ?)
            """,
            (group_text, feature, now_text),
        )
        await db.commit()
    return True


async def is_feature_allowed(event: MessageEvent, feature: str) -> bool:
    if not isinstance(event, GroupMessageEvent):
        return True
    return await is_group_feature_enabled(str(event.group_id), feature)


async def group_feature_status(group_id: str) -> dict[str, bool]:
    return {
        feature: await is_group_feature_enabled(group_id, feature)
        for feature in FEATURE_LABELS
    }
