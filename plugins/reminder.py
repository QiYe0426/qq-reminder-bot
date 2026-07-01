from __future__ import annotations

from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from nonebot import get_bot, get_driver, on_command, on_fullmatch, on_startswith
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent
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
from plugins.chime_service import (
    CHIME_MODE_HOURLY,
    format_chime_time,
    group_chime_status_text,
    init_chime_db,
    list_chime_targets,
    parse_chime_mode,
    set_chime_state,
    should_send_chime,
)
from plugins.reminder_service import (
    ReminderScope,
    cancel_reminder as cancel_reminder_record,
    create_reminder as create_reminder_record,
    due_reminders,
    format_reminder_list,
    init_reminder_db,
    list_reminders as list_reminder_records,
    mark_reminders_done,
)
HELP_TEXT = """猎bot菜单
基础
ping 检测bot是否在线
help/帮助/菜单/功能 查看菜单
@我 问题 / 猎宝，问题 群聊AI对话，会按需联网搜索
猎宝，问题 私聊AI对话

提醒
提醒 09:00 喝水 创建定时提醒
提醒 10分钟后喝水 创建倒计时提醒
提醒 2026-06-20 09:00 喝水 创建指定日期提醒
查看提醒 查看未完成提醒
取消提醒 1 取消指定提醒

智能陪伴
画像状态 查看本群智能陪伴状态
我的画像 查看本人在当前群的陪伴画像
画像记录名单 查看本群控制台已选择记录对象（管理员）
查看群友画像 @群友 查看指定群友画像（管理员）
群内注册/退出/自助更新已关闭，记录对象请在控制台选择

管理员
群功能状态 查看本群开关
开启/关闭群功能 AI对话 控制本群AI对话
开启/关闭群功能 提醒 控制本群提醒
开启/关闭群功能 日报 控制日报数据采集
开启/关闭群功能 陪伴画像 控制智能陪伴
开启/关闭群功能 调戏其他bot 控制对已标记bot的主动短回复
开启/关闭群功能 🎒常数回怼 控制158关键词表情包回复
启用/开启🎒常数报时 开启每小时58分报时
启用/开启🎒常数报时 每天两次 开启凌晨1:58和下午1:58报时
关闭🎒常数报时 关闭常数报时
采集状态 查看日报采集数量
查看采集 20 导出最近采集消息
预览日报 昨天 群号 生成日报预览txt
生成日报 昨天 群号 生成日报PDF
媒体识别状态 / 扫描媒体识别 管理素材识别
存储状态 查看服务器存储占用"""

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


def chime_target(event: MessageEvent) -> tuple[str, str]:
    if isinstance(event, GroupMessageEvent):
        return "group", str(event.group_id)
    return "private", str(event.user_id)


def get_connected_bot() -> Bot | None:
    try:
        return get_bot()
    except ValueError:
        logger.warning("No OneBot connection is active; skip scheduled sends.")
        return None


def reminder_scope(event: MessageEvent) -> ReminderScope:
    if isinstance(event, GroupMessageEvent):
        return ReminderScope(user_id=str(event.user_id), target_type="group", group_id=str(event.group_id))
    return ReminderScope(user_id=str(event.user_id), target_type="private")


async def create_reminder_reply(event: MessageEvent, raw_text: str) -> str:
    result = await create_reminder_record(reminder_scope(event), raw_text)
    return str(result.get("message") or "")


async def get_reminder_list_text(event: MessageEvent) -> str:
    return format_reminder_list(await list_reminder_records(str(event.user_id)))


async def cancel_reminder_by_id(event: MessageEvent, raw_id: str) -> str:
    raw_id = raw_id.strip()
    if not raw_id.isdigit():
        return "格式：取消提醒 1"
    result = await cancel_reminder_record(str(event.user_id), int(raw_id))
    return str(result.get("message") or "")


async def set_chime(event: MessageEvent, enabled: bool, mode: str = CHIME_MODE_HOURLY) -> str:
    target_type, target_id = chime_target(event)
    result = await set_chime_state(target_type, target_id, enabled, mode)
    return str(result.get("message") or "")


async def send_hourly_chimes() -> None:
    now = datetime.now()
    message = f"现在是{format_chime_time(now)}，吉时已到！"
    targets = await list_chime_targets()
    if not targets:
        return

    bot = get_connected_bot()
    if bot is None:
        return

    for item in targets:
        if not should_send_chime(str(item.get("mode") or ""), now):
            continue
        if item["target_type"] == "group":
            await bot.send_group_msg(group_id=int(str(item["target_id"])), message=message)
        else:
            await bot.send_private_msg(user_id=int(str(item["target_id"])), message=message)


async def send_due_reminders() -> None:
    reminders = await due_reminders()
    if not reminders:
        return

    bot = get_connected_bot()
    if bot is None:
        return

    sent_ids: list[int] = []

    for item in reminders:
        message = f"提醒：{item['content']}"
        if item["target_type"] == "group" and item["group_id"]:
            await bot.send_group_msg(group_id=int(str(item["group_id"])), message=message)
        else:
            await bot.send_private_msg(user_id=int(str(item["user_id"])), message=message)
        sent_ids.append(int(item["id"]))

    await mark_reminders_done(sent_ids)


@driver.on_startup
async def startup() -> None:
    await init_access_db()
    await init_reminder_db()
    await init_chime_db()
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
    reply = await create_reminder_reply(event, args.extract_plain_text())
    await add_reminder.finish(Message(reply))


@plain_add_reminder.handle()
async def handle_plain_add_reminder(event: MessageEvent) -> None:
    if not await is_feature_allowed(event, FEATURE_REMINDER):
        return
    text = event.get_plaintext().strip()
    raw_text = text.removeprefix("提醒").strip()
    reply = await create_reminder_reply(event, raw_text)
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
    lines.append(f"AI 对话：{'开启' if status.get('ai_chat') else '关闭'}")
    lines.append(f"日报：{'开启' if status.get('collector') else '关闭'}")
    lines.append(f"智能陪伴：{'开启' if status.get('companion') else '关闭'}")
    lines.append(f"调戏其他bot：{'开启' if status.get('bot_tease') else '关闭'}")
    lines.append(f"🎒常数回怼：{'开启' if status.get('constant_retort') else '关闭'}")
    lines.append(f"🎒常数报时：{await group_chime_status_text(event.group_id)}")
    await group_feature_status_cmd.finish(Message("\n".join(lines)))
