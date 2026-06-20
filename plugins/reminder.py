from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from nonebot import get_bot, get_driver, on_command, on_fullmatch, on_startswith
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, Message, MessageEvent
from nonebot.params import CommandArg


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
HELP_TEXT = """猎bot菜单
ping 检测bot是否在线
help 查看菜单
@我/猎宝，问题 群聊AI对话
猎宝，问题 私聊AI对话
提醒 09:00 喝水 创建定时提醒
提醒 10分钟后喝水 创建倒计时提醒
提醒 2026-06-20 09:00 喝水 创建指定日期提醒
查看提醒 查看未完成提醒
取消提醒 1 取消指定提醒
启用🎒常数报时 开启每小时58分报时
关闭🎒常数报时 关闭每小时58分报时"""

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
enable_hourly_chime = on_fullmatch("启用🎒常数报时", priority=5, block=True)
disable_hourly_chime = on_fullmatch("关闭🎒常数报时", priority=5, block=True)


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
                updated_at TEXT NOT NULL,
                PRIMARY KEY (target_type, target_id)
            )
            """
        )
        await db.commit()


def event_target(event: MessageEvent) -> tuple[str, str | None]:
    if isinstance(event, GroupMessageEvent):
        return "group", str(event.group_id)
    return "private", None


def chime_target(event: MessageEvent) -> tuple[str, str]:
    if isinstance(event, GroupMessageEvent):
        return "group", str(event.group_id)
    return "private", str(event.user_id)


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


async def set_hourly_chime(event: MessageEvent, enabled: bool) -> str:
    target_type, target_id = chime_target(event)
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO hourly_chime_settings
                (target_type, target_id, enabled, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(target_type, target_id) DO UPDATE SET
                enabled = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (target_type, target_id, 1 if enabled else 0, updated_at),
        )
        await db.commit()

    if enabled:
        return "已启用🎒常数报时，请静候佳音。"
    return "已关闭🎒常数报时，感恩遇见！"


async def send_hourly_chimes() -> None:
    now = datetime.now()
    message = f"现在是{now.strftime('%H')}:58，吉时已到！"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT target_type, target_id
            FROM hourly_chime_settings
            WHERE enabled = 1
            """
        )
        rows = await cursor.fetchall()

    if not rows:
        return

    bot = get_bot()
    for row in rows:
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

    bot = get_bot()
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
    reply = await create_reminder(event, args.extract_plain_text())
    await add_reminder.finish(Message(reply))


@plain_add_reminder.handle()
async def handle_plain_add_reminder(event: MessageEvent) -> None:
    text = event.get_plaintext().strip()
    raw_text = text.removeprefix("提醒").strip()
    reply = await create_reminder(event, raw_text)
    await plain_add_reminder.finish(Message(reply))


@list_reminders.handle()
async def handle_list_reminders(event: MessageEvent) -> None:
    reply = await get_reminder_list_text(event)
    await list_reminders.finish(Message(reply))


@plain_list_reminders.handle()
async def handle_plain_list_reminders(event: MessageEvent) -> None:
    reply = await get_reminder_list_text(event)
    await plain_list_reminders.finish(Message(reply))


@cancel_reminder.handle()
async def handle_cancel_reminder(event: Event, args: Message = CommandArg()) -> None:
    reply = await cancel_reminder_by_id(event, args.extract_plain_text())
    await cancel_reminder.finish(Message(reply))


@plain_cancel_reminder.handle()
async def handle_plain_cancel_reminder(event: MessageEvent) -> None:
    text = event.get_plaintext().strip()
    raw_id = text.removeprefix("取消提醒").strip()
    reply = await cancel_reminder_by_id(event, raw_id)
    await plain_cancel_reminder.finish(Message(reply))


@enable_hourly_chime.handle()
async def handle_enable_hourly_chime(event: MessageEvent) -> None:
    reply = await set_hourly_chime(event, True)
    await enable_hourly_chime.finish(Message(reply))


@disable_hourly_chime.handle()
async def handle_disable_hourly_chime(event: MessageEvent) -> None:
    reply = await set_hourly_chime(event, False)
    await disable_hourly_chime.finish(Message(reply))
