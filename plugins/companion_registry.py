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
)

driver = get_driver()

register_companion = on_fullmatch(REGISTER_COMMANDS, priority=5, block=True)
unregister_companion = on_fullmatch(UNREGISTER_COMMANDS, priority=5, block=True)
companion_status = on_fullmatch(STATUS_COMMANDS, priority=5, block=True)
companion_registered_list = on_fullmatch(LIST_COMMANDS, priority=5, block=True)

_db_ready = False


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sender_name(event: GroupMessageEvent) -> str | None:
    sender = getattr(event, "sender", None)
    if sender is None:
        return None
    card = str(getattr(sender, "card", "") or "").strip()
    nickname = str(getattr(sender, "nickname", "") or "").strip()
    return card or nickname or None


async def init_companion_db() -> None:
    global _db_ready
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS companion_registrations (
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                sender_name TEXT,
                consent_text TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                registered_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                revoked_at TEXT,
                PRIMARY KEY (group_id, user_id)
            )
            """
        )
        await db.commit()
    _db_ready = True


async def ensure_companion_db() -> None:
    if not _db_ready:
        await init_companion_db()


async def set_registration(
    *,
    group_id: str,
    user_id: str,
    display_name: str | None,
    consent_text: str,
    active: bool,
) -> None:
    await ensure_companion_db()
    timestamp = now_text()
    revoked_at = None if active else timestamp
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO companion_registrations (
                group_id,
                user_id,
                sender_name,
                consent_text,
                active,
                registered_at,
                updated_at,
                revoked_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_id, user_id) DO UPDATE SET
                sender_name = excluded.sender_name,
                consent_text = excluded.consent_text,
                active = excluded.active,
                registered_at = CASE
                    WHEN excluded.active = 1 AND companion_registrations.active = 0
                    THEN excluded.registered_at
                    ELSE companion_registrations.registered_at
                END,
                updated_at = excluded.updated_at,
                revoked_at = excluded.revoked_at
            """,
            (
                group_id,
                user_id,
                display_name,
                consent_text,
                1 if active else 0,
                timestamp,
                timestamp,
                revoked_at,
            ),
        )
        await db.commit()


async def is_companion_registered(group_id: str | int, user_id: str | int) -> bool:
    await ensure_companion_db()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT active
            FROM companion_registrations
            WHERE group_id = ? AND user_id = ?
            """,
            (str(group_id), str(user_id)),
        )
        row = await cursor.fetchone()
    return bool(row and row[0])


async def companion_registration_count(group_id: str | int) -> int:
    await ensure_companion_db()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM companion_registrations
            WHERE group_id = ? AND active = 1
            """,
            (str(group_id),),
        )
        row = await cursor.fetchone()
    return int(row[0] or 0) if row else 0


async def registered_members_text(group_id: str | int) -> str:
    await ensure_companion_db()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT user_id, sender_name, updated_at
            FROM companion_registrations
            WHERE group_id = ? AND active = 1
            ORDER BY updated_at DESC
            LIMIT 50
            """,
            (str(group_id),),
        )
        rows = await cursor.fetchall()

    if not rows:
        return "本群还没有群友注册陪伴画像。"

    lines = ["本群已注册陪伴画像的群友："]
    for row in rows:
        display_name = str(row["sender_name"] or "").strip() or str(row["user_id"])
        lines.append(f"- {display_name}（{row['user_id']}）")
    return "\n".join(lines)


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
        await register_companion.finish(Message("画像注册需要在群聊里本人发送。"))

    group_id = str(group_event.group_id)
    if not await is_group_feature_enabled(group_id, FEATURE_COMPANION):
        await register_companion.finish(Message("本群还没有开启陪伴画像，请管理员先发送：开启群功能 陪伴画像。"))

    consent_text = group_event.get_plaintext().strip()
    await set_registration(
        group_id=group_id,
        user_id=str(group_event.user_id),
        display_name=sender_name(group_event),
        consent_text=consent_text,
        active=True,
    )
    await register_companion.finish(
        Message(
            "已注册陪伴画像。之后猎宝只会在本群内基于你的聊天记录更新你的画像，"
            "你可以随时发送“退出猎宝画像”停止记录。"
        )
    )


@unregister_companion.handle()
async def handle_unregister_companion(event: Event) -> None:
    group_event = require_group(event)
    if group_event is None:
        await unregister_companion.finish(Message("画像退出需要在群聊里本人发送。"))

    await set_registration(
        group_id=str(group_event.group_id),
        user_id=str(group_event.user_id),
        display_name=sender_name(group_event),
        consent_text=group_event.get_plaintext().strip(),
        active=False,
    )
    await unregister_companion.finish(Message("已退出陪伴画像。之后猎宝不会再为你更新画像。"))


@companion_status.handle()
async def handle_companion_status(event: Event) -> None:
    group_event = require_group(event)
    if group_event is None:
        await companion_status.finish(Message("画像状态需要在群聊里查看。"))

    group_id = str(group_event.group_id)
    feature_enabled = await is_group_feature_enabled(group_id, FEATURE_COMPANION)
    registered = await is_companion_registered(group_id, group_event.user_id)
    registered_count = await companion_registration_count(group_id)
    lines = [
        "陪伴画像状态",
        f"本群功能：{'开启' if feature_enabled else '关闭'}",
        f"你的注册：{'已注册' if registered else '未注册'}",
        f"本群已注册人数：{registered_count}",
    ]
    if not registered:
        lines.append("注册口令：同意猎宝记录我")
    await companion_status.finish(Message("\n".join(lines)))


@companion_registered_list.handle()
async def handle_companion_registered_list(event: Event) -> None:
    if denial := admin_denial(event):
        await companion_registered_list.finish(Message(denial))

    group_event = require_group(event)
    if group_event is None:
        await companion_registered_list.finish(Message("注册名单需要在群聊里查看。"))

    await companion_registered_list.finish(Message(await registered_members_text(group_event.group_id)))
