from __future__ import annotations

from datetime import datetime
from pathlib import Path

import aiosqlite
from nonebot import get_driver, on_fullmatch
from nonebot.adapters.onebot.v11 import Event, GroupMessageEvent, Message

from plugins.access_control import (
    FEATURE_COMPANION,
    admin_denial,
    init_access_db,
    is_group_feature_enabled,
)


DB_PATH = Path("data/companion_memory.db")

REGISTER_COMMANDS = (
    "同意猎宝记录我",
    "注册猎宝画像",
    "注册陪伴画像",
    "开启我的画像",
)
UNREGISTER_COMMANDS = (
    "退出猎宝画像",
    "注销猎宝画像",
    "关闭我的画像",
)
STATUS_COMMANDS = (
    "画像状态",
    "我的画像状态",
    "陪伴状态",
)
LIST_COMMANDS = (
    "画像注册名单",
    "陪伴注册名单",
    "画像记录名单",
    "陪伴记录名单",
)

driver = get_driver()

register_companion = on_fullmatch(REGISTER_COMMANDS, priority=5, block=True)
unregister_companion = on_fullmatch(UNREGISTER_COMMANDS, priority=5, block=True)
companion_status = on_fullmatch(STATUS_COMMANDS, priority=5, block=True)
companion_target_list = on_fullmatch(LIST_COMMANDS, priority=5, block=True)

_db_ready = False


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def init_companion_db() -> None:
    global _db_ready
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS companion_targets (
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                display_name TEXT,
                enabled INTEGER NOT NULL DEFAULT 0,
                is_bot INTEGER NOT NULL DEFAULT 0,
                bot_keywords TEXT NOT NULL DEFAULT '[]',
                selected_by TEXT,
                selected_at TEXT,
                updated_at TEXT NOT NULL,
                disabled_at TEXT,
                PRIMARY KEY (group_id, user_id)
            )
            """
        )
        cursor = await db.execute("PRAGMA table_info(companion_targets)")
        columns = {str(row[1]) for row in await cursor.fetchall()}
        if "is_bot" not in columns:
            await db.execute("ALTER TABLE companion_targets ADD COLUMN is_bot INTEGER NOT NULL DEFAULT 0")
        if "bot_keywords" not in columns:
            await db.execute("ALTER TABLE companion_targets ADD COLUMN bot_keywords TEXT NOT NULL DEFAULT '[]'")
        await db.commit()
    _db_ready = True


async def ensure_companion_db() -> None:
    if not _db_ready:
        await init_companion_db()


async def set_companion_target(
    *,
    group_id: str,
    user_id: str,
    display_name: str | None = None,
    enabled: bool,
    selected_by: str | None = None,
    is_bot: bool | None = None,
    bot_keywords: str | None = None,
) -> None:
    await ensure_companion_db()
    timestamp = now_text()
    insert_is_bot = 1 if is_bot else 0
    insert_bot_keywords = bot_keywords if bot_keywords is not None else "[]"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO companion_targets (
                group_id,
                user_id,
                display_name,
                enabled,
                is_bot,
                bot_keywords,
                selected_by,
                selected_at,
                updated_at,
                disabled_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_id, user_id) DO UPDATE SET
                display_name = COALESCE(excluded.display_name, companion_targets.display_name),
                enabled = excluded.enabled,
                is_bot = CASE
                    WHEN ? = 1 THEN excluded.is_bot
                    ELSE companion_targets.is_bot
                END,
                bot_keywords = CASE
                    WHEN ? = 1 THEN excluded.bot_keywords
                    ELSE companion_targets.bot_keywords
                END,
                selected_by = COALESCE(excluded.selected_by, companion_targets.selected_by),
                selected_at = CASE
                    WHEN excluded.enabled = 1 AND companion_targets.enabled = 0
                    THEN excluded.selected_at
                    ELSE companion_targets.selected_at
                END,
                updated_at = excluded.updated_at,
                disabled_at = excluded.disabled_at
            """,
            (
                str(group_id),
                str(user_id),
                display_name,
                1 if enabled else 0,
                insert_is_bot,
                insert_bot_keywords,
                selected_by,
                timestamp if enabled else None,
                timestamp,
                None if enabled else timestamp,
                1 if is_bot is not None else 0,
                1 if bot_keywords is not None else 0,
            ),
        )
        await db.commit()


async def is_companion_target_enabled(group_id: str | int, user_id: str | int) -> bool:
    await ensure_companion_db()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT enabled
            FROM companion_targets
            WHERE group_id = ? AND user_id = ?
            """,
            (str(group_id), str(user_id)),
        )
        row = await cursor.fetchone()
    return bool(row and row[0])


async def companion_target_count(group_id: str | int) -> int:
    await ensure_companion_db()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM companion_targets
            WHERE group_id = ? AND enabled = 1
            """,
            (str(group_id),),
        )
        row = await cursor.fetchone()
    return int(row[0] or 0) if row else 0


async def companion_targets_text(group_id: str | int) -> str:
    await ensure_companion_db()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT user_id, display_name, updated_at
            FROM companion_targets
            WHERE group_id = ? AND enabled = 1
            ORDER BY updated_at DESC
            LIMIT 50
            """,
            (str(group_id),),
        )
        rows = await cursor.fetchall()

    if not rows:
        return "本群还没有在控制台选择允许记录的群友。"

    lines = ["本群已在控制台允许记录的群友："]
    for row in rows:
        display_name = str(row["display_name"] or "").strip() or str(row["user_id"])
        lines.append(f"- {display_name}（{row['user_id']}）")
    return "\n".join(lines)


async def companion_target_started_at(group_id: str | int, user_id: str | int) -> str | None:
    await ensure_companion_db()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT selected_at
            FROM companion_targets
            WHERE group_id = ? AND user_id = ? AND enabled = 1
            """,
            (str(group_id), str(user_id)),
        )
        row = await cursor.fetchone()
    return str(row[0]) if row and row[0] else None


def require_group(event: Event) -> GroupMessageEvent | None:
    return event if isinstance(event, GroupMessageEvent) else None


@driver.on_startup
async def startup() -> None:
    await init_access_db()
    await init_companion_db()


@register_companion.handle()
async def handle_register_companion(event: Event) -> None:
    group_event = require_group(event)
    if group_event is None:
        await register_companion.finish(Message("陪伴画像记录对象现在由控制台管理。"))

    group_id = str(group_event.group_id)
    if not await is_group_feature_enabled(group_id, FEATURE_COMPANION):
        await register_companion.finish(Message("本群还没有开启智能陪伴，请管理员先在控制台开启。"))

    await register_companion.finish(
        Message(
            "群内自助注册已关闭。请管理员在猎宝控制台的「群管理 -> 智能陪伴 -> 群友画像管理」里选择允许记录的群友。"
        )
    )


@unregister_companion.handle()
async def handle_unregister_companion(event: Event) -> None:
    group_event = require_group(event)
    if group_event is None:
        await unregister_companion.finish(Message("陪伴画像记录对象现在由控制台管理。"))

    await unregister_companion.finish(Message("群内自助退出已关闭。请管理员在猎宝控制台关闭记录或删除画像。"))


@companion_status.handle()
async def handle_companion_status(event: Event) -> None:
    group_event = require_group(event)
    if group_event is None:
        await companion_status.finish(Message("画像状态需要在群聊里查看。"))

    group_id = str(group_event.group_id)
    feature_enabled = await is_group_feature_enabled(group_id, FEATURE_COMPANION)
    target_enabled = await is_companion_target_enabled(group_id, group_event.user_id)
    target_count = await companion_target_count(group_id)
    lines = [
        "陪伴画像状态",
        f"本群功能：{'开启' if feature_enabled else '关闭'}",
        f"你的记录：{'控制台已开启' if target_enabled else '未开启'}",
        f"本群已选择记录人数：{target_count}",
    ]
    if not target_enabled:
        lines.append("记录对象现在由管理员在猎宝控制台选择。")
    await companion_status.finish(Message("\n".join(lines)))


@companion_target_list.handle()
async def handle_companion_target_list(event: Event) -> None:
    if denial := admin_denial(event):
        await companion_target_list.finish(Message(denial))

    group_event = require_group(event)
    if group_event is None:
        await companion_target_list.finish(Message("记录名单需要在群聊里查看。"))

    await companion_target_list.finish(Message(await companion_targets_text(group_event.group_id)))
