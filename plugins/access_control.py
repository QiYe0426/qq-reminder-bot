from __future__ import annotations

import os
from datetime import datetime
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

FEATURE_LABELS = {
    FEATURE_REMINDER: "提醒",
    FEATURE_COLLECTOR: "消息采集",
    FEATURE_COMPANION: "陪伴画像",
}

FEATURE_ALIASES = {
    "提醒": FEATURE_REMINDER,
    "定时提醒": FEATURE_REMINDER,
    "采集": FEATURE_COLLECTOR,
    "消息采集": FEATURE_COLLECTOR,
    "群消息采集": FEATURE_COLLECTOR,
    "群聊采集": FEATURE_COLLECTOR,
    "陪伴": FEATURE_COMPANION,
    "陪伴画像": FEATURE_COMPANION,
    "画像": FEATURE_COMPANION,
    "性格画像": FEATURE_COMPANION,
    "情感陪伴": FEATURE_COMPANION,
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
    normalized = raw_name.strip().lower()
    return FEATURE_ALIASES.get(normalized)


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
        return False
    return bool(row[0])


async def is_feature_allowed(event: MessageEvent, feature: str) -> bool:
    if not isinstance(event, GroupMessageEvent):
        return True
    return await is_group_feature_enabled(str(event.group_id), feature)


async def group_feature_status(group_id: str) -> dict[str, bool]:
    return {
        feature: await is_group_feature_enabled(group_id, feature)
        for feature in FEATURE_LABELS
    }
