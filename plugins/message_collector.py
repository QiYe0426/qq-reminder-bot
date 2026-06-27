from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import aiosqlite
from nonebot import get_driver, on_fullmatch, on_message, on_regex
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, Message, MessageEvent
from nonebot.log import logger
from nonebot.params import RegexGroup

from plugins.access_control import (
    FEATURE_COLLECTOR,
    admin_denial,
    init_access_db,
    is_group_feature_enabled,
)
from plugins.message_archive import DB_PATH, init_archive_db, save_group_message


driver = get_driver()
EXPORT_DIR = Path("data/exports")
EXPORT_ROUTE_PREFIX = "/hunterbot/exports"
EXPORT_BASE_URL_ENV = "EXPORT_BASE_URL"

message_collector = on_message(priority=30, block=False)
collector_status = on_fullmatch(("采集状态", "消息采集状态"), priority=5, block=True)
recent_collected_messages = on_regex(
    r"^查看采集\s+(\d{1,2})$",
    priority=5,
    block=True,
)


def clamp_recent_limit(raw_limit: str) -> int:
    try:
        limit = int(raw_limit)
    except ValueError:
        return 20
    return min(max(limit, 1), 50)


def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def display_sender(row: aiosqlite.Row) -> str:
    name = str(row["sender_name"] or "").strip()
    if name:
        return name
    if row["sub_type"] == "ai_reply":
        return "猎bot"
    return str(row["user_id"])


def export_file_url(filename: str) -> str:
    base_url = os.getenv(EXPORT_BASE_URL_ENV)
    if not base_url:
        port = os.getenv("PORT", "8080")
        base_url = f"http://127.0.0.1:{port}"
    return f"{base_url.rstrip('/')}{EXPORT_ROUTE_PREFIX}/{quote(filename)}"


def export_file_path(filename: str) -> Path:
    safe_filename = Path(filename).name
    return (EXPORT_DIR / safe_filename).resolve()


try:
    from fastapi import HTTPException
    from fastapi.responses import FileResponse
except Exception:  # pragma: no cover - FastAPI is provided by nonebot2[fastapi].
    HTTPException = None
    FileResponse = None


server_app = getattr(driver, "server_app", None)
if server_app is not None and HTTPException is not None and FileResponse is not None:

    @server_app.get(f"{EXPORT_ROUTE_PREFIX}/{{filename}}")
    async def serve_export_file(filename: str) -> FileResponse:
        file_path = export_file_path(filename)
        export_root = EXPORT_DIR.resolve()
        if not str(file_path).startswith(str(export_root)) or not file_path.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        return FileResponse(file_path, filename=file_path.name, media_type="text/plain")


async def recent_messages_text(event: Event, limit: int) -> tuple[str, str]:
    now_text = datetime.now().strftime("%Y%m%d-%H%M%S")
    if isinstance(event, GroupMessageEvent):
        scope = f"当前群最近{limit}条采集消息"
        where_clause = "WHERE group_id = ?"
        params: tuple[object, ...] = (str(event.group_id), limit)
        filename = f"message-archive-group-{event.group_id}-{now_text}.txt"
    else:
        scope = f"全部群最近{limit}条采集消息"
        where_clause = ""
        params = (limit,)
        filename = f"message-archive-all-{now_text}.txt"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT
                id,
                group_id,
                user_id,
                sender_name,
                sub_type,
                created_at,
                segment_types,
                plain_text,
                raw_message
            FROM collected_messages
            {where_clause}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            params,
        )
        rows = await cursor.fetchall()

        message_ids = [int(row["id"]) for row in rows]
        insights_by_message_id: dict[int, list[aiosqlite.Row]] = {}
        if message_ids:
            placeholders = ", ".join("?" for _ in message_ids)
            insight_cursor = await db.execute(
                f"""
                SELECT message_archive_id, insight_type, status, content
                FROM message_insights
                WHERE message_archive_id IN ({placeholders})
                ORDER BY id ASC
                """,
                tuple(message_ids),
            )
            insight_rows = await insight_cursor.fetchall()
            for insight_row in insight_rows:
                insights_by_message_id.setdefault(int(insight_row["message_archive_id"]), []).append(insight_row)

    if not rows:
        return filename, f"{scope}\n暂无记录。\n"

    lines = [
        scope,
        f"导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    sorted_rows = sorted(rows, key=lambda row: (row["created_at"], row["id"]))
    for row in sorted_rows:
        sub_type = row["sub_type"] or "normal"
        text = normalize_text(row["plain_text"])
        if not text:
            text = f"[{row['segment_types']}]"
        lines.extend(
            [
                "-" * 60,
                f"id: {row['id']}",
                f"group_id: {row['group_id']}",
                f"user_id: {row['user_id']}",
                f"sender: {display_sender(row)}",
                f"time: {row['created_at']}",
                f"type: {sub_type}",
                f"segments: {row['segment_types']}",
                "text:",
                text,
            ]
        )
        raw_message = normalize_text(row["raw_message"])
        if raw_message and raw_message != text:
            lines.extend(["raw_message:", raw_message])
        insight_rows = insights_by_message_id.get(int(row["id"]), [])
        if insight_rows:
            lines.append("insights:")
            for insight_row in insight_rows:
                lines.append(
                    f"- {insight_row['insight_type']} / {insight_row['status']}：{insight_row['content'] or ''}"
                )
        lines.append("")

    return filename, "\n".join(lines)


async def send_text_file(bot: Bot, event: Event, filename: str, content: str) -> None:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    file_path = export_file_path(filename)
    file_path.write_text(content, encoding="utf-8")

    file_value = export_file_url(filename)
    if isinstance(event, GroupMessageEvent):
        try:
            await bot.call_api(
                "upload_group_file",
                group_id=event.group_id,
                file=file_value,
                name=filename,
            )
        except Exception:
            await bot.call_api(
                "upload_group_file",
                group_id=event.group_id,
                file=str(file_path),
                name=filename,
            )
        return

    try:
        await bot.call_api(
            "upload_private_file",
            user_id=int(event.get_user_id()),
            file=file_value,
            name=filename,
        )
    except Exception:
        await bot.call_api(
            "upload_private_file",
            user_id=int(event.get_user_id()),
            file=str(file_path),
            name=filename,
        )


async def collector_status_text(event: Event) -> str:
    today_start = datetime.now().strftime("%Y-%m-%d 00:00:00")
    if isinstance(event, GroupMessageEvent):
        group_filter = "WHERE group_id = ?"
        today_filter = "WHERE group_id = ? AND created_at >= ?"
        params: tuple[str, ...] = (str(event.group_id),)
        today_params: tuple[str, ...] = (str(event.group_id), today_start)
        scope = f"当前群：{event.group_id}"
    else:
        group_filter = ""
        today_filter = "WHERE created_at >= ?"
        params = ()
        today_params = (today_start,)
        scope = "全部已采集群"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        total_cursor = await db.execute(
            f"""
            SELECT COUNT(*) AS total_count, MAX(created_at) AS latest_created_at
            FROM collected_messages
            {group_filter}
            """,
            params,
        )
        total_row = await total_cursor.fetchone()

        today_cursor = await db.execute(
            f"""
            SELECT COUNT(*) AS today_count
            FROM collected_messages
            {today_filter}
            """,
            today_params,
        )
        today_row = await today_cursor.fetchone()

    total_count = int(total_row["total_count"] or 0) if total_row else 0
    today_count = int(today_row["today_count"] or 0) if today_row else 0
    latest_created_at = total_row["latest_created_at"] if total_row else None

    lines = [
        "消息采集状态",
        scope,
        f"累计消息：{total_count}",
        f"今日消息：{today_count}",
        f"最新消息：{latest_created_at or '暂无'}",
    ]
    return "\n".join(lines)


@driver.on_startup
async def startup() -> None:
    await init_access_db()
    await init_archive_db()


@message_collector.handle()
async def handle_message_collect(event: MessageEvent) -> None:
    if not isinstance(event, GroupMessageEvent):
        return

    if not await is_group_feature_enabled(str(event.group_id), FEATURE_COLLECTOR):
        return

    try:
        await save_group_message(event)
    except Exception:
        logger.exception("Failed to save group message")


@collector_status.handle()
async def handle_collector_status(event: Event) -> None:
    if denial := admin_denial(event):
        await collector_status.finish(Message(denial))
    await collector_status.finish(Message(await collector_status_text(event)))


@recent_collected_messages.handle()
async def handle_recent_collected_messages(
    bot: Bot,
    event: Event,
    matched_groups: tuple[str, ...] = RegexGroup(),
) -> None:
    if denial := admin_denial(event):
        await recent_collected_messages.finish(Message(denial))
    limit = clamp_recent_limit(matched_groups[0] if matched_groups else "20")
    filename, content = await recent_messages_text(event, limit)
    try:
        await send_text_file(bot, event, filename, content)
    except Exception:
        logger.exception("Failed to upload collected messages text file")
        await recent_collected_messages.finish(
            Message(f"txt已生成但发送失败：{filename}。请检查 NapCat 是否支持文件上传。")
        )
    await recent_collected_messages.finish()
