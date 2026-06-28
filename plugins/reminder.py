from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from nonebot import get_bot, get_driver, on_command, on_fullmatch, on_startswith
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, Message, MessageEvent
from nonebot.log import logger
from nonebot.params import CommandArg

from plugins.access_control import (
    FEATURE_LABELS,
    FEATURE_REMINDER,
    admin_denial,
    group_feature_status,
    init_access_db,
    is_feature_allowed,
    normalize_feature_name,
    set_group_feature,
)


DB_PATH = Path("data/reminders.db")
TIME_FORMAT = "%Y-%m-%d %H:%M"
TIME_ONLY_FORMAT = "%H:%M"
CHIME_MODE_HOURLY = "hourly"
CHIME_MODE_TWICE_DAILY = "twice_daily"
CHIME_MODE_LABELS = {
    CHIME_MODE_HOURLY: "每小时58分",
    CHIME_MODE_TWICE_DAILY: "每天凌晨1:58和下午1:58",
}
TWICE_DAILY_CHIME_HOURS = {1, 13}
NUMBER_TEXT = r"\d+|[零〇一二两俩三四五六七八九十百千]+"
RELATIVE_TIME_PATTERN = re.compile(
    rf"^(?:(?P<days>{NUMBER_TEXT})\s*天\s*)?"
    rf"(?:(?P<hours>{NUMBER_TEXT})\s*(?:小时|个小时)\s*)?"
    rf"(?:(?P<minutes>{NUMBER_TEXT})\s*分钟\s*)?"
    r"后\s*(?P<content>.+)$"
)
HELP_TEXT = """猎bot菜单
ping 检测bot是否在线
help 查看菜单
@我/猎宝，问题 群聊AI对话
猎宝，问题 私聊AI对话
开启群功能 陪伴画像 开启本群陪伴画像（管理员）
同意猎宝记录我 注册本人陪伴画像
画像状态 查看本群陪伴画像状态
我的画像 查看本人陪伴画像
更新我的画像 手动更新本人画像
重置我的画像 清空本人画像但保留注册
删除我的画像 删除本人画像并退出注册
提醒 09:00 喝水 创建定时提醒
提醒 10分钟后喝水 创建倒计时提醒
提醒 2026-06-20 09:00 喝水 创建指定日期提醒
查看提醒 查看未完成提醒
取消提醒 1 取消指定提醒
启用/开启🎒常数报时 开启每小时58分报时（管理员）
启用/开启🎒常数报时 每天两次 开启凌晨1:58和下午1:58报时（管理员）
关闭🎒常数报时 关闭常数报时（管理员）"""

driver = get_driver()
scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

ping = on_command("ping", priority=5, block=True)
help_menu = on_command("help", aliases={"帮助", "菜单", "功能", "使用帮助"}, priority=5, block=True)
add_reminder = on_command("提醒", aliases={"remind"}, priority=5, block=True)
list_reminders = on_command("查看提醒", aliases={"提醒列表"}, priority=5, block=True)
cancel_reminder = on_command("取消提醒", priority=5, block=True)
plain_ping = on_fullmatch("ping", ignorecase=True, priority=5, block=True)
plain_help_menu = on_fullmatch(("help", "Help", "HELP", "帮助", "菜单", "功能", "使用帮助"), priority=5, block=True)
plain_add_reminder = on_startswith(("提醒 ", "提醒　"), priority=5, block=True)
plain_list_reminders = on_fullmatch(("查看提醒", "提醒列表"), priority=5, block=True)
plain_cancel_reminder = on_startswith(("取消提醒 ", "取消提醒　"), priority=5, block=True)
enable_hourly_chime = on_startswith(("启用🎒常数报时", "开启🎒常数报时"), priority=5, block=True)
disable_hourly_chime = on_fullmatch("关闭🎒常数报时", priority=5, block=True)
enable_group_feature = on_startswith(("开启群功能 ", "开启群功能　"), priority=4, block=True)
disable_group_feature = on_startswith(("关闭群功能 ", "关闭群功能　"), priority=4, block=True)
group_feature_status_cmd = on_fullmatch("群功能状态", priority=4, block=True)


async def init_db() -> None:
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
        await ensure_column(
            db,
            "hourly_chime_settings",
            "mode",
            "TEXT NOT NULL DEFAULT 'hourly'",
        )
        await db.commit()


async def ensure_column(
    db: aiosqlite.Connection,
    table_name: str,
    column_name: str,
    column_definition: str,
) -> None:
    cursor = await db.execute(f"PRAGMA table_info({table_name})")
    columns = await cursor.fetchall()
    if any(column[1] == column_name for column in columns):
        return
    await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")


def event_target(event: MessageEvent) -> tuple[str, str | None]:
    if isinstance(event, GroupMessageEvent):
        return "group", str(event.group_id)
    return "private", None


def chime_target(event: MessageEvent) -> tuple[str, str]:
    if isinstance(event, GroupMessageEvent):
        return "group", str(event.group_id)
    return "private", str(event.user_id)


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


def get_connected_bot() -> Bot | None:
    try:
        return get_bot()
    except ValueError:
        logger.warning("No OneBot connection is active; skip scheduled sends.")
        return None


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


def parse_reminder(text: str) -> tuple[datetime, str] | None:
    text = text.strip()

    match = RELATIVE_TIME_PATTERN.match(text)
    if match:
        days = parse_number(match.group("days"))
        hours = parse_number(match.group("hours"))
        minutes = parse_number(match.group("minutes"))
        content = match.group("content").strip()
        if content and (days or hours or minutes):
            remind_at = datetime.now() + timedelta(
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

    now = datetime.now()
    try:
        parsed_time = datetime.strptime(raw_time, TIME_ONLY_FORMAT).time()
    except ValueError:
        return None

    remind_at = now.replace(
        hour=parsed_time.hour,
        minute=parsed_time.minute,
        second=0,
        microsecond=0,
    )
    if remind_at <= now:
        remind_at += timedelta(days=1)

    return remind_at, content


async def create_reminder(event: MessageEvent, raw_text: str) -> str:
    parsed = parse_reminder(raw_text)
    if parsed is None:
        return "格式：提醒 2026-06-20 09:00 喝水 或 提醒 09:00 喝水 或 提醒 10分钟后喝水"

    remind_at, content = parsed
    if remind_at <= datetime.now():
        return "提醒时间需要晚于现在。"

    target_type, group_id = event_target(event)
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO reminders
                (user_id, group_id, target_type, remind_at, content, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(event.user_id),
                group_id,
                target_type,
                remind_at.strftime(TIME_FORMAT),
                content,
                created_at,
            ),
        )
        await db.commit()
        reminder_id = cursor.lastrowid

    return f"已创建提醒 #{reminder_id}：{remind_at.strftime(TIME_FORMAT)} {content}"


async def get_reminder_list_text(event: MessageEvent) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, remind_at, content
            FROM reminders
            WHERE done = 0 AND user_id = ?
            ORDER BY remind_at ASC
            LIMIT 10
            """,
            (str(event.user_id),),
        )
        rows = await cursor.fetchall()

    if not rows:
        return "你现在没有未完成提醒。"

    lines = ["未完成提醒："]
    for row in rows:
        lines.append(f"#{row['id']} {row['remind_at']} {row['content']}")

    return "\n".join(lines)


async def cancel_reminder_by_id(event: Event, raw_id: str) -> str:
    raw_id = raw_id.strip()
    if not raw_id.isdigit():
        return "格式：取消提醒 1"

    reminder_id = int(raw_id)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE reminders
            SET done = 1
            WHERE id = ? AND user_id = ? AND done = 0
            """,
            (reminder_id, str(event.get_user_id())),
        )
        await db.commit()
        changed = cursor.rowcount

    if changed:
        return f"已取消提醒 #{reminder_id}。"
    return "没有找到这个未完成提醒，或它不属于你。"


async def set_chime(event: MessageEvent, enabled: bool, mode: str = CHIME_MODE_HOURLY) -> str:
    target_type, target_id = chime_target(event)
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO hourly_chime_settings
                (target_type, target_id, enabled, mode, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(target_type, target_id) DO UPDATE SET
                enabled = excluded.enabled,
                mode = excluded.mode,
                updated_at = excluded.updated_at
            """,
            (target_type, target_id, 1 if enabled else 0, mode, updated_at),
        )
        await db.commit()

    if enabled:
        return f"已启用🎒常数报时：{CHIME_MODE_LABELS.get(mode, mode)}，请静候佳音。"
    return "已关闭🎒常数报时，感恩遇见！"


async def get_chime_status_text(event: GroupMessageEvent) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT enabled, mode
            FROM hourly_chime_settings
            WHERE target_type = 'group' AND target_id = ?
            """,
            (str(event.group_id),),
        )
        row = await cursor.fetchone()

    if row is None or not bool(row[0]):
        return "关闭"
    return CHIME_MODE_LABELS.get(str(row[1]), str(row[1]))


async def send_hourly_chimes() -> None:
    now = datetime.now()
    message = f"现在是{format_chime_time(now)}，吉时已到！"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT target_type, target_id, mode
            FROM hourly_chime_settings
            WHERE enabled = 1
            """
        )
        rows = await cursor.fetchall()

    if not rows:
        return

    bot = get_connected_bot()
    if bot is None:
        return

    for row in rows:
        if not should_send_chime(row["mode"], now):
            continue
        if row["target_type"] == "group":
            await bot.send_group_msg(group_id=int(row["target_id"]), message=message)
        else:
            await bot.send_private_msg(user_id=int(row["target_id"]), message=message)


async def send_due_reminders() -> None:
    now_text = datetime.now().strftime(TIME_FORMAT)

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
        reminders = await cursor.fetchall()

    if not reminders:
        return

    bot = get_connected_bot()
    if bot is None:
        return

    sent_ids: list[int] = []

    for item in reminders:
        message = f"提醒：{item['content']}"
        if item["target_type"] == "group" and item["group_id"]:
            await bot.send_group_msg(group_id=int(item["group_id"]), message=message)
        else:
            await bot.send_private_msg(user_id=int(item["user_id"]), message=message)
        sent_ids.append(item["id"])

    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "UPDATE reminders SET done = 1 WHERE id = ?",
            [(item_id,) for item_id in sent_ids],
        )
        await db.commit()


@driver.on_startup
async def startup() -> None:
    await init_access_db()
    await init_db()
    scheduler.add_job(send_due_reminders, "interval", seconds=30, id="send_due_reminders")
    scheduler.add_job(send_hourly_chimes, "cron", minute=58, second=5, id="send_hourly_chimes")
    scheduler.start()


@driver.on_shutdown
async def shutdown() -> None:
    scheduler.shutdown(wait=False)


@ping.handle()
async def handle_ping() -> None:
    await ping.finish(Message("pong"))


@plain_ping.handle()
async def handle_plain_ping() -> None:
    await plain_ping.finish(Message("pong"))


@help_menu.handle()
async def handle_help_menu() -> None:
    await help_menu.finish(Message(HELP_TEXT))


@plain_help_menu.handle()
async def handle_plain_help_menu() -> None:
    await plain_help_menu.finish(Message(HELP_TEXT))


@add_reminder.handle()
async def handle_add_reminder(event: MessageEvent, args: Message = CommandArg()) -> None:
    if not await is_feature_allowed(event, FEATURE_REMINDER):
        return
    reply = await create_reminder(event, args.extract_plain_text())
    await add_reminder.finish(Message(reply))


@plain_add_reminder.handle()
async def handle_plain_add_reminder(event: MessageEvent) -> None:
    if not await is_feature_allowed(event, FEATURE_REMINDER):
        return
    text = event.get_plaintext().strip()
    raw_text = text.removeprefix("提醒").strip()
    reply = await create_reminder(event, raw_text)
    await plain_add_reminder.finish(Message(reply))


@list_reminders.handle()
async def handle_list_reminders(event: MessageEvent) -> None:
    if not await is_feature_allowed(event, FEATURE_REMINDER):
        return
    reply = await get_reminder_list_text(event)
    await list_reminders.finish(Message(reply))


@plain_list_reminders.handle()
async def handle_plain_list_reminders(event: MessageEvent) -> None:
    if not await is_feature_allowed(event, FEATURE_REMINDER):
        return
    reply = await get_reminder_list_text(event)
    await plain_list_reminders.finish(Message(reply))


@cancel_reminder.handle()
async def handle_cancel_reminder(event: MessageEvent, args: Message = CommandArg()) -> None:
    if not await is_feature_allowed(event, FEATURE_REMINDER):
        return
    reply = await cancel_reminder_by_id(event, args.extract_plain_text())
    await cancel_reminder.finish(Message(reply))


@plain_cancel_reminder.handle()
async def handle_plain_cancel_reminder(event: MessageEvent) -> None:
    if not await is_feature_allowed(event, FEATURE_REMINDER):
        return
    text = event.get_plaintext().strip()
    raw_id = text.removeprefix("取消提醒").strip()
    reply = await cancel_reminder_by_id(event, raw_id)
    await plain_cancel_reminder.finish(Message(reply))


@enable_hourly_chime.handle()
async def handle_enable_hourly_chime(event: MessageEvent) -> None:
    if denial := admin_denial(event):
        await enable_hourly_chime.finish(Message(denial))
    mode = parse_chime_mode(event.get_plaintext().strip())
    if mode is None:
        await enable_hourly_chime.finish(Message("格式：启用🎒常数报时 每小时 或 启用🎒常数报时 每天两次"))
    reply = await set_chime(event, True, mode)
    await enable_hourly_chime.finish(Message(reply))


@disable_hourly_chime.handle()
async def handle_disable_hourly_chime(event: MessageEvent) -> None:
    if denial := admin_denial(event):
        await disable_hourly_chime.finish(Message(denial))
    reply = await set_chime(event, False)
    await disable_hourly_chime.finish(Message(reply))


async def set_current_group_feature(event: MessageEvent, raw_text: str, enabled: bool) -> str:
    if not isinstance(event, GroupMessageEvent):
        return "这个命令需要在群聊里使用。"

    parts = raw_text.split(maxsplit=1)
    if len(parts) != 2:
        return "格式：开启群功能 提醒 / 关闭群功能 提醒"

    feature = normalize_feature_name(parts[1])
    if feature is None:
        available_features = "、".join(FEATURE_LABELS.values())
        return f"未知功能。可用功能：{available_features}。"

    await set_group_feature(str(event.group_id), feature, enabled)
    action = "开启" if enabled else "关闭"
    return f"已{action}本群功能：{FEATURE_LABELS[feature]}。"


@enable_group_feature.handle()
async def handle_enable_group_feature(event: MessageEvent) -> None:
    if denial := admin_denial(event):
        await enable_group_feature.finish(Message(denial))
    reply = await set_current_group_feature(event, event.get_plaintext().strip(), True)
    await enable_group_feature.finish(Message(reply))


@disable_group_feature.handle()
async def handle_disable_group_feature(event: MessageEvent) -> None:
    if denial := admin_denial(event):
        await disable_group_feature.finish(Message(denial))
    reply = await set_current_group_feature(event, event.get_plaintext().strip(), False)
    await disable_group_feature.finish(Message(reply))


@group_feature_status_cmd.handle()
async def handle_group_feature_status(event: MessageEvent) -> None:
    if denial := admin_denial(event):
        await group_feature_status_cmd.finish(Message(denial))
    if not isinstance(event, GroupMessageEvent):
        await group_feature_status_cmd.finish(Message("这个命令需要在群聊里使用。"))

    status = await group_feature_status(str(event.group_id))
    lines = ["本群功能状态："]
    for feature, label in FEATURE_LABELS.items():
        lines.append(f"{label}：{'开启' if status[feature] else '关闭'}")
    lines.append(f"🎒常数报时：{await get_chime_status_text(event)}")
    await group_feature_status_cmd.finish(Message("\n".join(lines)))
