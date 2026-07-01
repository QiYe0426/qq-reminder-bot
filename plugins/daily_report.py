from __future__ import annotations

import asyncio
import json
import os
import re
import textwrap
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from nonebot import get_bot, get_driver, on_regex
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, Message, PrivateMessageEvent
from nonebot.log import logger
from nonebot.params import RegexGroup

from plugins.access_control import (
    DB_PATH as ACCESS_DB_PATH,
    FEATURE_COLLECTOR,
    FEATURE_DAILY_REPORT_AUTO,
    admin_denial,
    admin_user_ids,
    init_access_db,
)
from plugins.message_archive import DB_PATH, init_archive_db
from plugins.message_collector import display_sender, export_file_path, export_file_url, normalize_text, send_text_file


load_dotenv(".env.local")

REPORT_DIR = Path("data/reports")
SUMMARY_MODEL_ENV = "SUMMARY_MODEL"
SUMMARY_API_KEY_ENV = "SUMMARY_API_KEY"
SUMMARY_BASE_URL_ENV = "SUMMARY_BASE_URL"
SUMMARY_TIMEOUT_SECONDS_ENV = "SUMMARY_TIMEOUT_SECONDS"
SUMMARY_CHUNK_MESSAGES_ENV = "SUMMARY_CHUNK_MESSAGES"
SUMMARY_MAX_INPUT_CHARS_ENV = "SUMMARY_MAX_INPUT_CHARS"
DAILY_REPORT_ENABLED_ENV = "DAILY_REPORT_ENABLED"
DAILY_REPORT_GROUP_IDS_ENV = "DAILY_REPORT_GROUP_IDS"
DAILY_REPORT_SEND_TIME_ENV = "DAILY_REPORT_SEND_TIME"
DAILY_REPORT_TIMEZONE_ENV = "DAILY_REPORT_TIMEZONE"
DAILY_REPORT_STARTUP_GRACE_MINUTES_ENV = "DAILY_REPORT_STARTUP_GRACE_MINUTES"
DEFAULT_SUMMARY_MODEL = "deepseek-v4-pro"
DEFAULT_SUMMARY_TIMEOUT_SECONDS = 90
DEFAULT_SUMMARY_CHUNK_MESSAGES = 80
DEFAULT_SUMMARY_MAX_INPUT_CHARS = 24000
DEFAULT_STARTUP_GRACE_MINUTES = 120
RUNNING_REPORT_STALE_MINUTES = 180
REPORT_CHAT_CHUNK_CHARS = 1200
REPORT_CHAT_MAX_CHARS = 4800
REPORT_DAY_START_HOUR = 4

driver = get_driver()
scheduler = AsyncIOScheduler(timezone=os.getenv(DAILY_REPORT_TIMEZONE_ENV, "Asia/Shanghai"))

daily_report = on_regex(
    r"^生成日报(?:\s+(\S+))?(?:\s+(\d+))?$",
    priority=5,
    block=True,
)
daily_report_preview = on_regex(
    r"^预览日报(?:\s+(\S+))?(?:\s+(\d+))?$",
    priority=5,
    block=True,
)
daily_report_test_send = on_regex(
    r"^测试日报(?:\s+(\S+))?(?:\s+(\d+))?$",
    priority=5,
    block=True,
)


def parse_report_date(raw_value: str | None) -> date:
    value = (raw_value or "今天").strip()
    today = date.today()
    if value in {"今天", "今日", "today"}:
        return today
    if value in {"昨天", "昨日", "yesterday"}:
        return today - timedelta(days=1)
    if value in {"前天"}:
        return today - timedelta(days=2)
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("日期格式应为 今天、昨天、前天 或 2026-06-27。") from exc


def day_range(target_date: date) -> tuple[str, str]:
    start_at = datetime.combine(target_date, datetime.min.time()).replace(hour=REPORT_DAY_START_HOUR)
    end_at = start_at + timedelta(days=1)
    return start_at.strftime("%Y-%m-%d %H:%M:%S"), end_at.strftime("%Y-%m-%d %H:%M:%S")


def get_int_env(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    raw_value = os.getenv(name)
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        return default
    value = max(value, minimum)
    if maximum is not None:
        value = min(value, maximum)
    return value


def parse_id_list(raw_value: str) -> list[str]:
    ids: list[str] = []
    for item in raw_value.replace("，", ",").split(","):
        item = item.strip()
        if item and item.isdigit() and item not in ids:
            ids.append(item)
    return ids


def daily_report_enabled() -> bool:
    return os.getenv(DAILY_REPORT_ENABLED_ENV, "").strip() in {"1", "true", "True", "yes", "on"}


def report_group_ids() -> list[str]:
    return parse_id_list(os.getenv(DAILY_REPORT_GROUP_IDS_ENV, ""))


def automatic_report_group_source() -> str:
    return "env" if report_group_ids() else "collector"


async def init_daily_report_run_db() -> None:
    await init_access_db()
    async with aiosqlite.connect(ACCESS_DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_report_runs (
                group_id TEXT NOT NULL,
                target_date TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (group_id, target_date)
            )
            """
        )
        await db.commit()


async def collector_enabled_group_ids() -> list[str]:
    await init_access_db()
    async with aiosqlite.connect(ACCESS_DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT group_id
            FROM group_feature_settings
            WHERE feature = ? AND enabled = 1
            ORDER BY group_id ASC
            """,
            (FEATURE_COLLECTOR,),
        )
        rows = await cursor.fetchall()
    return [str(row[0]) for row in rows]


async def collector_auto_report_group_ids() -> list[str]:
    await init_access_db()
    async with aiosqlite.connect(ACCESS_DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT collector.group_id
            FROM group_feature_settings collector
            LEFT JOIN group_feature_settings auto
              ON auto.group_id = collector.group_id AND auto.feature = ?
            WHERE collector.feature = ?
              AND collector.enabled = 1
              AND COALESCE(auto.enabled, 1) = 1
            ORDER BY collector.group_id ASC
            """,
            (FEATURE_DAILY_REPORT_AUTO, FEATURE_COLLECTOR),
        )
        rows = await cursor.fetchall()
    return [str(row[0]) for row in rows]


async def automatic_report_group_ids() -> list[str]:
    explicit_group_ids = report_group_ids()
    if explicit_group_ids:
        return explicit_group_ids
    return await collector_auto_report_group_ids()


def skip_reason_text(reason: str) -> str:
    return {
        "sent": "已经生成过，本轮不重复发送",
        "running": "上一轮仍在生成中",
    }.get(reason, reason or "未知原因")


async def daily_report_already_sent(group_id: str, target_date: date) -> bool:
    await init_daily_report_run_db()
    async with aiosqlite.connect(ACCESS_DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT status
            FROM daily_report_runs
            WHERE group_id = ? AND target_date = ?
            """,
            (str(group_id), target_date.isoformat()),
        )
        row = await cursor.fetchone()
    return bool(row and row[0] == "sent")


async def mark_daily_report_run(group_id: str, target_date: date, status: str, error: str = "") -> None:
    await init_daily_report_run_db()
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(ACCESS_DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO daily_report_runs (group_id, target_date, status, error, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(group_id, target_date) DO UPDATE SET
                status = excluded.status,
                error = excluded.error,
                updated_at = excluded.updated_at
            """,
            (str(group_id), target_date.isoformat(), status, error[:1000], updated_at),
        )
        await db.commit()


async def begin_daily_report_run(group_id: str, target_date: date) -> str | None:
    await init_daily_report_run_db()
    now = datetime.now()
    updated_at = now.strftime("%Y-%m-%d %H:%M:%S")
    stale_before = (now - timedelta(minutes=RUNNING_REPORT_STALE_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(ACCESS_DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT OR IGNORE INTO daily_report_runs (group_id, target_date, status, error, updated_at)
            VALUES (?, ?, 'running', '', ?)
            """,
            (str(group_id), target_date.isoformat(), updated_at),
        )
        if cursor.rowcount:
            await db.commit()
            return None

        cursor = await db.execute(
            """
            SELECT status, updated_at
            FROM daily_report_runs
            WHERE group_id = ? AND target_date = ?
            """,
            (str(group_id), target_date.isoformat()),
        )
        row = await cursor.fetchone()
        status = str(row[0]) if row else "unknown"
        last_updated = str(row[1]) if row and row[1] else ""
        if status == "sent":
            await db.commit()
            return "sent"
        if status == "running" and last_updated >= stale_before:
            await db.commit()
            return "running"

        await db.execute(
            """
            UPDATE daily_report_runs
            SET status = 'running', error = '', updated_at = ?
            WHERE group_id = ? AND target_date = ?
            """,
            (updated_at, str(group_id), target_date.isoformat()),
        )
        await db.commit()
    return None


def summary_model() -> str:
    return os.getenv(SUMMARY_MODEL_ENV, DEFAULT_SUMMARY_MODEL).strip() or DEFAULT_SUMMARY_MODEL


def summary_api_key() -> str | None:
    return os.getenv(SUMMARY_API_KEY_ENV) or os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")


def summary_base_url() -> str | None:
    return os.getenv(SUMMARY_BASE_URL_ENV) or os.getenv("DEEPSEEK_BASE_URL") or os.getenv("OPENAI_BASE_URL")


def summary_timeout_seconds() -> int:
    return get_int_env(SUMMARY_TIMEOUT_SECONDS_ENV, DEFAULT_SUMMARY_TIMEOUT_SECONDS, minimum=5)


def summary_chunk_messages() -> int:
    return get_int_env(SUMMARY_CHUNK_MESSAGES_ENV, DEFAULT_SUMMARY_CHUNK_MESSAGES, minimum=20, maximum=200)


def summary_max_input_chars() -> int:
    return get_int_env(SUMMARY_MAX_INPUT_CHARS_ENV, DEFAULT_SUMMARY_MAX_INPUT_CHARS, minimum=4000, maximum=80000)


def daily_report_send_time() -> tuple[int, int]:
    raw_value = os.getenv(DAILY_REPORT_SEND_TIME_ENV, "04:00").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw_value)
    if not match:
        return 8, 30
    hour = min(max(int(match.group(1)), 0), 23)
    minute = min(max(int(match.group(2)), 0), 59)
    return hour, minute


def daily_report_startup_grace_minutes() -> int:
    return get_int_env(
        DAILY_REPORT_STARTUP_GRACE_MINUTES_ENV,
        DEFAULT_STARTUP_GRACE_MINUTES,
        minimum=0,
        maximum=1440,
    )


def startup_catchup_target_date(now: datetime | None = None) -> date | None:
    now = now or datetime.now()
    hour, minute = daily_report_send_time()
    scheduled_at = datetime.combine(now.date(), datetime.min.time()).replace(hour=hour, minute=minute)
    grace_minutes = daily_report_startup_grace_minutes()
    if grace_minutes <= 0:
        return None
    if scheduled_at <= now <= scheduled_at + timedelta(minutes=grace_minutes):
        return now.date() - timedelta(days=1)
    return None


def is_private_report_event(event: Event) -> bool:
    return isinstance(event, PrivateMessageEvent)


def resolve_group_id(event: Event, raw_group_id: str | None, command_example: str = "生成日报 昨天 548901561") -> str:
    if raw_group_id and raw_group_id.isdigit():
        return raw_group_id
    raise ValueError(f"私聊使用日报命令时请带群号，例如：{command_example}")


def safe_filename_date(target_date: date) -> str:
    return target_date.strftime("%Y%m%d")


def display_report_date(target_date: date) -> str:
    return f"{target_date.year}年{target_date.month}月{target_date.day}日"


def safe_report_filename_part(value: str, fallback: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n]+', "_", normalize_text(value))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    return cleaned[:60] or fallback


async def get_group_name(bot: Bot | None, group_id: str) -> str:
    if bot is None:
        return f"群{group_id}"
    try:
        info = await bot.call_api("get_group_info", group_id=int(group_id), no_cache=False)
    except Exception:
        logger.warning(f"Failed to get group info for {group_id}")
        return f"群{group_id}"
    if isinstance(info, dict):
        name = str(info.get("group_name") or info.get("group_remark") or "").strip()
        if name:
            return name
    return f"群{group_id}"


def report_filename_base(target_date: date, group_name: str, group_id: str) -> str:
    date_part = display_report_date(target_date)
    group_part = safe_report_filename_part(group_name, f"群{group_id}")
    return f"{date_part} {group_part}_{group_id}"


def report_file_path(filename: str) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    return (REPORT_DIR / Path(filename).name).resolve()


async def send_existing_file(bot: Bot, event: Event, source_path: Path, filename: str) -> None:
    export_path = export_file_path(filename)
    export_path.parent.mkdir(parents=True, exist_ok=True)
    export_path.write_bytes(source_path.read_bytes())
    file_value = export_file_url(filename)

    if isinstance(event, GroupMessageEvent):
        try:
            await bot.call_api("upload_group_file", group_id=event.group_id, file=file_value, name=filename)
        except Exception:
            await bot.call_api("upload_group_file", group_id=event.group_id, file=str(export_path), name=filename)
        return

    try:
        await bot.call_api("upload_private_file", user_id=int(event.get_user_id()), file=file_value, name=filename)
    except Exception:
        await bot.call_api("upload_private_file", user_id=int(event.get_user_id()), file=str(export_path), name=filename)


def compact_line(text: str, limit: int = 220) -> str:
    text = normalize_text(text)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def report_chat_body(markdown: str) -> str:
    lines: list[str] = []
    for line in normalize_text(markdown).splitlines():
        stripped = line.strip()
        if stripped.startswith("##") and "附录" in stripped:
            break
        lines.append(line)
    body = "\n".join(lines).strip()
    return body or "日报内容为空。"


def report_pdf_body(markdown: str) -> str:
    lines: list[str] = []
    skipping_preview_block = False
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("##") and "附录" in stripped:
            break
        if stripped == "# 猎bot日报预览":
            skipping_preview_block = True
            continue
        if skipping_preview_block and stripped.startswith("## "):
            skipping_preview_block = False
        if skipping_preview_block:
            continue
        lines.append(line)
    body = "\n".join(lines).strip()
    return body or "日报内容为空。"


def split_report_chat_chunks(markdown: str) -> list[str]:
    body = report_chat_body(markdown)
    if len(body) > REPORT_CHAT_MAX_CHARS:
        body = (
            body[:REPORT_CHAT_MAX_CHARS].rstrip()
            + "\n\n（内容较长，聊天里先显示前半部分，完整日报见 Markdown/PDF 文件。）"
        )

    chunks: list[str] = []
    current_lines: list[str] = []
    current_len = 0
    for line in body.splitlines():
        line_len = len(line) + 1
        if line_len > REPORT_CHAT_CHUNK_CHARS:
            if current_lines:
                chunks.append("\n".join(current_lines).strip())
                current_lines = []
                current_len = 0
            for start in range(0, len(line), REPORT_CHAT_CHUNK_CHARS):
                chunks.append(line[start : start + REPORT_CHAT_CHUNK_CHARS].strip())
            continue
        if current_lines and current_len + line_len > REPORT_CHAT_CHUNK_CHARS:
            chunks.append("\n".join(current_lines).strip())
            current_lines = [line]
            current_len = line_len
            continue
        current_lines.append(line)
        current_len += line_len

    if current_lines:
        chunks.append("\n".join(current_lines).strip())
    return [chunk for chunk in chunks if chunk]


async def send_report_chunks(matcher, title: str, markdown: str) -> None:
    chunks = split_report_chat_chunks(markdown)
    total = len(chunks)
    for index, chunk in enumerate(chunks, start=1):
        heading = title if total == 1 else f"{title}（{index}/{total}）"
        await matcher.send(Message(f"{heading}\n\n{chunk}"))


async def send_private_report_chunks(bot: Bot, user_id: str, title: str, markdown: str) -> None:
    chunks = split_report_chat_chunks(markdown)
    total = len(chunks)
    for index, chunk in enumerate(chunks, start=1):
        heading = title if total == 1 else f"{title}（{index}/{total}）"
        await bot.call_api(
            "send_private_msg",
            user_id=int(user_id),
            message=f"{heading}\n\n{chunk}",
        )


def parse_json_value(raw_value: str | None) -> object | None:
    if not raw_value:
        return None
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError:
        return None


def insight_source_label(insight_type: str) -> str:
    return {
        "image": "图片识别",
        "emoji": "表情包识别",
        "link": "链接解析",
        "forward": "聊天记录展开",
        "record": "语音识别",
        "file": "文件识别",
        "reply": "回复引用",
        "video": "视频消息",
        "location": "位置消息",
        "share": "分享卡片",
        "contact": "名片消息",
        "poke": "戳一戳",
        "music": "音乐分享",
        "anonymous": "匿名消息",
        "json": "JSON卡片",
        "xml": "XML卡片",
    }.get(insight_type, insight_type)


def extract_forward_messages(raw_result: str | None) -> list[dict[str, object]]:
    value = parse_json_value(raw_result)
    if not isinstance(value, dict):
        return []
    messages = value.get("messages")
    if not isinstance(messages, list):
        return []
    return [item for item in messages if isinstance(item, dict)]


def normalize_insight_row(row: aiosqlite.Row) -> dict[str, object]:
    return {
        "message_archive_id": int(row["message_archive_id"]),
        "insight_type": str(row["insight_type"]),
        "status": str(row["status"]),
        "content": normalize_text(row["content"]),
        "raw_result": row["raw_result"],
        "error": row["error"],
    }


def remove_urls(text: str) -> str:
    return normalize_text(re.sub(r"https?://\S+", "", text)).strip()


def clean_insight_content_for_report(insight_type: str, content: str) -> str:
    content = normalize_text(content)
    if insight_type in {"image", "emoji"}:
        return remove_urls(content)
    return content


def format_media_report_line(insight_type: str, row: aiosqlite.Row, content: str) -> str:
    sender = display_sender(row)
    sent_at = str(row["created_at"])
    label = insight_source_label(insight_type)
    content = compact_line(content, 700)
    if insight_type == "record":
        return f"  <{label}> 发送人={sender}；时间={sent_at}；转写内容：{content}"
    if insight_type == "file":
        return f"  <{label}> 发送人={sender}；时间={sent_at}；{content}"
    return f"  <{label}> 发送人={sender}；时间={sent_at}；{content}"


def keyword_scores(text: str) -> Counter[str]:
    scores: Counter[str] = Counter()
    rules = {
        "待办/安排": r"待办|安排|明天|今天|今晚|下午|上午|提醒|记得|开会|截止|ddl|deadline",
        "链接/资料": r"https?://|链接|资料|文档|文章|视频|公众号|小红书|B站|bilibili",
        "图片/表情": r"\[图片|图片|截图|表情|表情包|照片",
        "文件/附件": r"\[文件|文件消息|压缩包|文档|表格|PPT|PDF|附件",
        "语音内容": r"\[语音|语音消息|转写|录音",
        "回复/引用": r"\[回复|回复引用|引用ID|引用QQ",
        "视频/位置": r"\[视频|视频消息|位置消息|定位|坐标",
        "问题/求助": r"\?|？|怎么|如何|为什么|求助|有没有|谁知道",
        "AI对话": r"ai_reply|猎bot",
        "争议/决策": r"但是|不过|不同意|争议|决定|结论|方案|选择|确认",
    }
    for label, pattern in rules.items():
        if re.search(pattern, text, re.IGNORECASE):
            scores[label] += 1
    return scores


def append_message_line(lines: list[str], row: aiosqlite.Row, insights: list[dict[str, object]]) -> None:
    sender = display_sender(row)
    sub_type = row["sub_type"] or "normal"
    text = normalize_text(row["plain_text"])
    if not text:
        text = f"[{row['segment_types']}]"

    lines.append(f"- `{row['created_at']}` **{sender}** ({sub_type})：{compact_line(text)}")

    for insight in insights:
        insight_type = str(insight["insight_type"])
        status = str(insight["status"])
        content = clean_insight_content_for_report(insight_type, str(insight["content"] or ""))
        if insight_type == "forward" and status == "ready":
            forward_messages = extract_forward_messages(str(insight["raw_result"] or ""))
            lines.append(f"  - {insight_source_label(insight_type)} / {status}：{len(forward_messages)} 条聊天记录子消息")
            for fallback_index, item in enumerate(forward_messages[:12], start=1):
                index = item.get("index") or fallback_index
                sent_at = item.get("time", "")
                sender_name = item.get("sender", "未知发送者")
                child_text = compact_line(str(item.get("text", "")), 180)
                time_part = f"[{sent_at}] " if sent_at else ""
                lines.append(f"    - [forward_message#{index}] {time_part}{sender_name}: {child_text}")
            if len(forward_messages) > 12:
                lines.append(f"    - ...还有 {len(forward_messages) - 12} 条聊天记录子消息")
            continue

        if content:
            lines.append(f"  - {insight_source_label(insight_type)} / {status}：{compact_line(content, 260)}")
        else:
            lines.append(f"  - {insight_source_label(insight_type)} / {status}")


def message_to_summary_input(row: aiosqlite.Row, insights: list[dict[str, object]]) -> str:
    sender = display_sender(row)
    sub_type = row["sub_type"] or "normal"
    text = normalize_text(row["plain_text"])
    if not text:
        text = f"[{row['segment_types']}]"

    parts = [f"[{row['created_at']}] {sender} ({sub_type}): {compact_line(text, 500)}"]
    for insight in insights:
        insight_type = str(insight["insight_type"])
        status = str(insight["status"])
        content = clean_insight_content_for_report(insight_type, str(insight["content"] or ""))
        if insight_type == "forward" and status == "ready":
            forward_messages = extract_forward_messages(str(insight["raw_result"] or ""))
            parts.append(f"  <聊天记录展开，共{len(forward_messages)}条，以下为子消息>")
            for fallback_index, item in enumerate(forward_messages[:40], start=1):
                index = item.get("index") or fallback_index
                sent_at = item.get("time", "")
                sender_name = item.get("sender", "未知发送者")
                child_text = compact_line(str(item.get("text", "")), 300)
                parts.append(f"  [forward_message#{index}] [{sent_at}] {sender_name}: {child_text}")
            if len(forward_messages) > 40:
                parts.append(f"  <聊天记录还有{len(forward_messages) - 40}条未放入本块>")
            continue
        if content:
            label = insight_source_label(insight_type)
            if insight_type in {"image", "emoji", "record", "file", "reply", "video", "location", "share", "contact", "poke", "music", "anonymous"}:
                parts.append(format_media_report_line(insight_type, row, content))
            else:
                parts.append(f"  <{label} / {status}> {compact_line(content, 700)}")
        else:
            parts.append(f"  <{insight_source_label(insight_type)} / {status}>")
    return "\n".join(parts)


def build_summary_inputs(
    rows: list[aiosqlite.Row],
    insights_by_message_id: dict[int, list[dict[str, object]]],
) -> list[str]:
    return [
        message_to_summary_input(row, insights_by_message_id.get(int(row["id"]), []))
        for row in rows
    ]


def chunk_summary_inputs(inputs: list[str]) -> list[str]:
    chunk_size = summary_chunk_messages()
    max_chars = summary_max_input_chars()
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0

    for item in inputs:
        item_length = len(item)
        if current and (len(current) >= chunk_size or current_length + item_length > max_chars):
            chunks.append("\n\n".join(current))
            current = []
            current_length = 0
        current.append(item)
        current_length += item_length
    if current:
        chunks.append("\n\n".join(current))
    return chunks


async def call_summary_ai(messages: list[dict[str, str]]) -> str:
    api_key = summary_api_key()
    if not api_key:
        raise RuntimeError(f"missing {SUMMARY_API_KEY_ENV} / DEEPSEEK_API_KEY / OPENAI_API_KEY")

    from openai import AsyncOpenAI

    base_url = summary_base_url()
    client = AsyncOpenAI(api_key=api_key, base_url=base_url) if base_url else AsyncOpenAI(api_key=api_key)
    response = await asyncio.wait_for(
        client.chat.completions.create(
            model=summary_model(),
            messages=messages,
        ),
        timeout=summary_timeout_seconds(),
    )
    content = (response.choices[0].message.content or "").strip()
    if not content:
        raise RuntimeError("empty summary response")
    return content


async def summarize_chunk(chunk_text: str, index: int, total: int) -> str:
    system_prompt = (
        "你是群聊日报信息整理助手。"
        "只根据用户提供的群消息和素材识别结果提取事实，不要编造。"
        "看到 [forward_message#N] 时，必须理解为合并聊天记录里的子消息，不是当前群直接消息。"
        "图片、表情包、链接、文件、语音、回复、视频、位置、分享卡片、名片、戳一戳、音乐和匿名消息都属于可总结素材。"
        "输出使用中文，结构清晰，保留重要原文线索。"
    )
    user_prompt = f"""这是当天群聊消息的第 {index}/{total} 块。

请提取：
1. 本块主要话题
2. 关键事实和结论
3. 待办/时间/人物
4. 重要链接、图片、表情包、文件、语音、回复、视频、位置和聊天记录内容
5. 争议或需要跟进的点

消息如下：

{chunk_text}
"""
    return await call_summary_ai(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
    )


async def synthesize_final_report(
    *,
    group_id: str,
    target_date: date,
    stats_text: str,
    chunk_summaries: list[str],
) -> str:
    system_prompt = (
        "你是群聊日报主编。"
        "需要把分块摘要合并成给管理员看的日报。"
        "不要把合并聊天记录里的子消息说成当前群直接发言；如引用它们，请标注来自聊天记录。"
        "图片、表情包、链接、文件、语音、回复、视频、位置、分享卡片、名片、戳一戳、音乐和匿名消息都属于日报素材，要合理归类。"
        "不要编造未提供的信息。"
    )
    user_prompt = f"""请根据以下信息生成最终日报。

群号：{group_id}
日期：{target_date.strftime('%Y-%m-%d')}
统计范围：{day_range(target_date)[0]} 至 {day_range(target_date)[1]}

统计信息：
{stats_text}

分块摘要：
{chr(10).join(f"--- 分块 {idx + 1} ---{chr(10)}{summary}" for idx, summary in enumerate(chunk_summaries))}

输出结构：
# 群聊日报
## 今日重点
## 分话题总结
## 重要链接和素材
## 待办与跟进
## 风险/争议/低置信度
## 值得回看的原文线索

格式要求：
- 可以使用标题和列表。
- 不要使用 **加粗**、表格或复杂 Markdown。
- 不要在正文开头重复输出群号、群名、日期；这些信息已经在 PDF 文件名中体现。
"""
    return await call_summary_ai(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
    )


async def fetch_report_rows(group_id: str, target_date: date) -> tuple[list[aiosqlite.Row], dict[int, list[dict[str, object]]]]:
    start_at, end_at = day_range(target_date)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
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
            WHERE group_id = ? AND created_at >= ? AND created_at < ?
            ORDER BY created_at ASC, id ASC
            """,
            (group_id, start_at, end_at),
        )
        rows = await cursor.fetchall()

        message_ids = [int(row["id"]) for row in rows]
        insights_by_message_id: dict[int, list[dict[str, object]]] = defaultdict(list)
        if message_ids:
            placeholders = ", ".join("?" for _ in message_ids)
            insight_cursor = await db.execute(
                f"""
                SELECT message_archive_id, insight_type, status, content, raw_result, error
                FROM message_insights
                WHERE message_archive_id IN ({placeholders})
                ORDER BY message_archive_id ASC, id ASC
                """,
                tuple(message_ids),
            )
            insight_rows = await insight_cursor.fetchall()
            for insight_row in insight_rows:
                insights_by_message_id[int(insight_row["message_archive_id"])].append(
                    normalize_insight_row(insight_row)
                )

    return rows, insights_by_message_id


def build_report_content(
    *,
    group_id: str,
    group_name: str | None = None,
    target_date: date,
    rows: list[aiosqlite.Row],
    insights_by_message_id: dict[int, list[dict[str, object]]],
) -> str:
    start_at, end_at = day_range(target_date)
    lines = [
        f"# 猎bot日报预览",
        "",
        f"- 群号：{group_id}",
        f"- 群名：{group_name or f'群{group_id}'}",
        f"- 日期：{target_date.strftime('%Y-%m-%d')}",
        f"- 统计范围：{start_at} 至 {end_at}",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 消息数：{len(rows)}",
        "",
    ]

    if not rows:
        lines.extend(["## 概览", "", "当天没有采集到消息。", ""])
        return "\n".join(lines)

    sender_counts: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()
    insight_counts: Counter[str] = Counter()
    topic_scores: Counter[str] = Counter()

    for row in rows:
        sender_counts[display_sender(row)] += 1
        parsed_types = parse_json_value(row["segment_types"])
        if isinstance(parsed_types, list):
            for segment_type in parsed_types:
                type_counts[str(segment_type)] += 1
        else:
            type_counts[str(row["segment_types"])] += 1

        text_for_score = normalize_text(row["plain_text"])
        for insight in insights_by_message_id.get(int(row["id"]), []):
            insight_type = str(insight["insight_type"])
            insight_counts[f"{insight_type}/{insight['status']}"] += 1
            text_for_score += "\n" + str(insight["content"] or "")
        topic_scores.update(keyword_scores(text_for_score))

    lines.extend(
        [
            "## 概览",
            "",
            f"- 活跃发言人：{len(sender_counts)}",
            f"- 主要消息类型：{', '.join(f'{key}:{value}' for key, value in type_counts.most_common(8)) or '无'}",
            f"- 识别结果：{', '.join(f'{key}:{value}' for key, value in insight_counts.most_common(8)) or '无'}",
            f"- 初步话题线索：{', '.join(f'{key}:{value}' for key, value in topic_scores.most_common(8)) or '无'}",
            "",
            "## 活跃发言人",
            "",
        ]
    )
    for sender, count in sender_counts.most_common(12):
        lines.append(f"- {sender}: {count} 条")

    lines.extend(["", "## 时间线", ""])
    for row in rows:
        append_message_line(lines, row, insights_by_message_id.get(int(row["id"]), []))

    lines.extend(
        [
            "",
            "## 后续 AI 总结输入说明",
            "",
            "- 普通行来自当前群当天直接消息。",
            "- `图片识别`、`表情包识别`、`链接解析` 是对应消息的补充素材。",
            "- `[forward_message#N]` 表示合并聊天记录里的子消息，不是当前群直接发送的新消息。",
        ]
    )

    return "\n".join(lines)


def build_report_stats_text(
    rows: list[aiosqlite.Row],
    insights_by_message_id: dict[int, list[dict[str, object]]],
) -> str:
    if not rows:
        return "当天没有采集到消息。"

    sender_counts: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()
    insight_counts: Counter[str] = Counter()
    topic_scores: Counter[str] = Counter()
    for row in rows:
        sender_counts[display_sender(row)] += 1
        parsed_types = parse_json_value(row["segment_types"])
        if isinstance(parsed_types, list):
            for segment_type in parsed_types:
                type_counts[str(segment_type)] += 1
        else:
            type_counts[str(row["segment_types"])] += 1
        text_for_score = normalize_text(row["plain_text"])
        for insight in insights_by_message_id.get(int(row["id"]), []):
            insight_counts[f"{insight['insight_type']}/{insight['status']}"] += 1
            text_for_score += "\n" + str(insight["content"] or "")
        topic_scores.update(keyword_scores(text_for_score))

    lines = [
        f"消息数：{len(rows)}",
        f"活跃发言人：{len(sender_counts)}",
        f"发言人TOP：{', '.join(f'{key}:{value}' for key, value in sender_counts.most_common(10)) or '无'}",
        f"消息类型：{', '.join(f'{key}:{value}' for key, value in type_counts.most_common(10)) or '无'}",
        f"识别结果：{', '.join(f'{key}:{value}' for key, value in insight_counts.most_common(10)) or '无'}",
        f"初步话题线索：{', '.join(f'{key}:{value}' for key, value in topic_scores.most_common(10)) or '无'}",
    ]
    return "\n".join(lines)


async def generate_daily_report_text(
    group_id: str,
    target_date: date,
    group_name: str | None = None,
) -> tuple[str, str]:
    await init_archive_db()
    rows, insights_by_message_id = await fetch_report_rows(group_id, target_date)
    content = build_report_content(
        group_id=group_id,
        group_name=group_name,
        target_date=target_date,
        rows=rows,
        insights_by_message_id=insights_by_message_id,
    )
    filename = report_filename_base(target_date, group_name or f"群{group_id}", group_id) + ".txt"
    return filename, content


async def generate_ai_daily_report_markdown(
    group_id: str,
    target_date: date,
    group_name: str | None = None,
) -> tuple[str, str, str]:
    await init_archive_db()
    rows, insights_by_message_id = await fetch_report_rows(group_id, target_date)
    preview_content = build_report_content(
        group_id=group_id,
        group_name=group_name,
        target_date=target_date,
        rows=rows,
        insights_by_message_id=insights_by_message_id,
    )
    filename_base = report_filename_base(target_date, group_name or f"群{group_id}", group_id)

    if not rows:
        return f"{filename_base}.md", preview_content, preview_content

    summary_inputs = build_summary_inputs(rows, insights_by_message_id)
    chunks = chunk_summary_inputs(summary_inputs)
    stats_text = build_report_stats_text(rows, insights_by_message_id)
    chunk_summaries: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        chunk_summaries.append(await summarize_chunk(chunk, index, len(chunks)))

    final_summary = await synthesize_final_report(
        group_id=group_id,
        target_date=target_date,
        stats_text=stats_text,
        chunk_summaries=chunk_summaries,
    )
    content = "\n\n".join(
        [
            final_summary,
            "## 统计信息",
            stats_text,
            "## 附录：结构化时间线",
            preview_content,
        ]
    )
    return f"{filename_base}.md", content, preview_content


def find_pdf_font_path() -> str | None:
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
    ]
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return None


def strip_inline_markdown(text: str) -> str:
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    return text.replace("---", "").strip()


def markdown_to_pdf_blocks(markdown: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        stripped_line = line.strip()
        if stripped_line in {"**群号：**", "**群名：**", "**日期：**"}:
            continue
        if re.fullmatch(r"\*\*(群号|群名|日期)[：:][^*]+\*\*", stripped_line):
            continue
        if stripped_line.startswith(("- 群号：", "- 群名：", "- 日期：")):
            continue
        if line.startswith("# "):
            blocks.append(("h1", strip_inline_markdown(line.removeprefix("# "))))
            blocks.append(("blank", ""))
        elif line.startswith("## "):
            blocks.append(("h2", strip_inline_markdown(line.removeprefix("## "))))
            blocks.append(("blank", ""))
        elif line.startswith("### "):
            blocks.append(("h3", strip_inline_markdown(line.removeprefix("### "))))
        elif line.strip().startswith("- "):
            blocks.append(("body", "- " + strip_inline_markdown(line.strip().removeprefix("- "))))
        elif line.strip():
            blocks.append(("body", strip_inline_markdown(line)))
        else:
            blocks.append(("blank", ""))
    return blocks


def write_pdf_report(markdown: str, pdf_path: Path) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    font_name = "STSong-Light"
    font_path = find_pdf_font_path()
    if font_path:
        try:
            font_name = "HunterBotFont"
            pdfmetrics.registerFont(TTFont(font_name, font_path))
        except Exception:
            logger.warning(f"Failed to register PDF font {font_path}; falling back to STSong-Light")
            font_name = "STSong-Light"
    if font_name == "STSong-Light":
        pdfmetrics.registerFont(UnicodeCIDFont(font_name))

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(pdf_path), pagesize=A4)
    width, height = A4
    left = 48
    top = height - 48
    max_width_chars = 52
    y = top

    c.setTitle(pdf_path.stem)
    for style, line in markdown_to_pdf_blocks(markdown):
        if style == "blank":
            y -= 8
            continue
        font_size = {"h1": 16, "h2": 13, "h3": 11}.get(style, 10)
        line_height = int(font_size * 1.65)
        if y < 48:
            c.showPage()
            y = top
        c.setFont(font_name, font_size)
        wrapped = textwrap.wrap(line, width=max_width_chars, replace_whitespace=False) if line else [""]
        for piece in wrapped:
            if y < 48:
                c.showPage()
                c.setFont(font_name, font_size)
                y = top
            c.drawString(left, y, piece)
            y -= line_height
        if style in {"h1", "h2"}:
            y -= 6
    c.save()


async def generate_daily_report_files(
    group_id: str,
    target_date: date,
    group_name: str | None = None,
) -> tuple[str, str, str]:
    markdown_filename, markdown_content, _preview_content = await generate_ai_daily_report_markdown(
        group_id,
        target_date,
        group_name=group_name,
    )
    markdown_path = report_file_path(markdown_filename)
    markdown_path.write_text(markdown_content, encoding="utf-8")

    pdf_filename = markdown_filename.removesuffix(".md") + ".pdf"
    pdf_path = report_file_path(pdf_filename)
    try:
        write_pdf_report(report_pdf_body(markdown_content), pdf_path)
        return markdown_filename, markdown_content, pdf_filename
    except Exception:
        logger.exception("Failed to generate PDF report")
        return markdown_filename, markdown_content, ""


class PrivateReportEvent:
    def __init__(self, user_id: str) -> None:
        self._user_id = user_id

    def get_user_id(self) -> str:
        return self._user_id


async def send_daily_report_notice(bot: Bot, message: str) -> None:
    admin_ids = sorted(admin_user_ids())
    if not admin_ids:
        logger.warning("Daily report notice skipped: no admins configured")
        return
    for admin_id in admin_ids:
        try:
            await bot.call_api("send_private_msg", user_id=int(admin_id), message=message)
        except Exception:
            logger.exception(f"Failed to send daily report notice to admin {admin_id}")


async def send_daily_report_to_admins(
    bot: Bot,
    group_id: str,
    target_date: date,
    group_name: str | None = None,
) -> str:
    group_name = group_name or await get_group_name(bot, group_id)
    markdown_filename, markdown_content, pdf_filename = await generate_daily_report_files(
        group_id,
        target_date,
        group_name=group_name,
    )
    admin_ids = sorted(admin_user_ids())
    if not admin_ids:
        logger.warning("Daily report skipped: no admins configured")
        return pdf_filename

    if not pdf_filename:
        message = f"群 {group_id} {target_date.strftime('%Y-%m-%d')} 日报生成失败：PDF 未生成。"
        for admin_id in admin_ids:
            try:
                await bot.call_api("send_private_msg", user_id=int(admin_id), message=message)
            except Exception:
                logger.exception(f"Failed to send daily report failure notice to admin {admin_id}")
        raise RuntimeError("PDF 未生成")

    for admin_id in admin_ids:
        event = PrivateReportEvent(admin_id)
        try:
            await send_existing_file(bot, event, report_file_path(pdf_filename), pdf_filename)
            await bot.call_api(
                "send_private_msg",
                user_id=int(admin_id),
                message=f"群 {group_id} {target_date.strftime('%Y-%m-%d')} 日报已生成：{pdf_filename}",
            )
        except Exception:
            logger.exception(f"Failed to send daily report to admin {admin_id}")
    return pdf_filename


async def scheduled_daily_report_job() -> None:
    if not daily_report_enabled():
        return
    target_date = date.today() - timedelta(days=1)
    await run_automatic_daily_reports(target_date, reason="cron")


async def run_automatic_daily_reports(target_date: date, reason: str) -> None:
    groups = await automatic_report_group_ids()
    if not groups:
        logger.warning("Daily report skipped: no configured or collector-enabled groups")
        return
    try:
        bot = get_bot()
    except Exception:
        logger.exception("Daily report skipped: no bot instance")
        return

    logger.info(
        f"Daily report automatic run started: reason={reason}, "
        f"target_date={target_date.isoformat()}, groups={','.join(groups)}"
    )
    source_text = "环境变量 DAILY_REPORT_GROUP_IDS" if automatic_report_group_source() == "env" else "控制台自动发送开关"
    await send_daily_report_notice(
        bot,
        "\n".join(
            [
                "自动日报开始",
                f"触发方式：{reason}",
                f"日报日期：{target_date.isoformat()}",
                f"群数量：{len(groups)}",
                f"群来源：{source_text}",
                f"群号：{', '.join(groups)}",
            ]
        ),
    )
    sent_count = 0
    skipped_count = 0
    failed_count = 0
    for group_id in groups:
        group_name = await get_group_name(bot, group_id)
        skip_reason = await begin_daily_report_run(group_id, target_date)
        if skip_reason:
            skipped_count += 1
            logger.info(
                f"Daily report skipped: status={skip_reason}, group={group_id}, "
                f"target_date={target_date.isoformat()}"
            )
            await send_daily_report_notice(
                bot,
                f"自动日报跳过：{group_name}（{group_id}）\n日期：{target_date.isoformat()}\n原因：{skip_reason_text(skip_reason)}",
            )
            continue
        try:
            await send_daily_report_notice(
                bot,
                f"自动日报开始生成：{group_name}（{group_id}）\n日期：{target_date.isoformat()}",
            )
            await send_daily_report_to_admins(bot, group_id, target_date, group_name=group_name)
            await mark_daily_report_run(group_id, target_date, "sent")
            sent_count += 1
            logger.info(f"Daily report sent: group={group_id}, target_date={target_date.isoformat()}")
        except Exception as exc:
            await mark_daily_report_run(group_id, target_date, "failed", repr(exc))
            failed_count += 1
            logger.exception(f"Daily report job failed for group {group_id}")
            await send_daily_report_notice(
                bot,
                f"自动日报失败：{group_name}（{group_id}）\n日期：{target_date.isoformat()}\n错误：{repr(exc)[:500]}",
            )
    await send_daily_report_notice(
        bot,
        "\n".join(
            [
                "自动日报结束",
                f"日报日期：{target_date.isoformat()}",
                f"成功：{sent_count}",
                f"跳过：{skipped_count}",
                f"失败：{failed_count}",
            ]
        ),
    )


@driver.on_startup
async def startup_daily_report() -> None:
    if not daily_report_enabled():
        return
    await init_daily_report_run_db()
    hour, minute = daily_report_send_time()
    grace_minutes = daily_report_startup_grace_minutes()
    if not scheduler.running:
        scheduler.start()
    scheduler.add_job(
        scheduled_daily_report_job,
        "cron",
        hour=hour,
        minute=minute,
        id="hunterbot_daily_report",
        replace_existing=True,
        misfire_grace_time=max(60, grace_minutes * 60) if grace_minutes > 0 else 60,
        coalesce=True,
    )
    logger.info(
        f"Daily report scheduled at {hour:02d}:{minute:02d}, "
        f"startup_grace_minutes={grace_minutes}"
    )
    catchup_date = startup_catchup_target_date()
    if catchup_date:
        logger.warning(
            f"Daily report startup catch-up queued: target_date={catchup_date.isoformat()}"
        )
        asyncio.create_task(run_automatic_daily_reports(catchup_date, reason="startup_catchup"))


@daily_report_preview.handle()
async def handle_daily_report_preview(
    bot: Bot,
    event: Event,
    matched_groups: tuple[str, ...] = RegexGroup(),
) -> None:
    if not is_private_report_event(event):
        await daily_report_preview.finish()
    if denial := admin_denial(event):
        await daily_report_preview.finish(Message(denial))

    raw_date = matched_groups[0] if len(matched_groups) >= 1 else None
    raw_group_id = matched_groups[1] if len(matched_groups) >= 2 else None
    try:
        target_date = parse_report_date(raw_date)
        group_id = resolve_group_id(event, raw_group_id, "预览日报 今天 548901561")
    except ValueError as exc:
        await daily_report_preview.finish(Message(str(exc)))

    group_name = await get_group_name(bot, group_id)
    filename, content = await generate_daily_report_text(group_id, target_date, group_name=group_name)
    try:
        await send_text_file(bot, event, filename, content)
    except Exception:
        logger.exception("Failed to upload daily report preview")
        await daily_report_preview.finish(Message(f"日报预览已生成但发送失败：{filename}。请检查 NapCat 是否支持文件上传。"))

    date_arg = (raw_date or "今天").strip() or target_date.strftime("%Y-%m-%d")
    await daily_report_preview.finish(Message(f"若想查看完整日报，请发送：生成日报 {date_arg} {group_id}"))


@daily_report.handle()
async def handle_daily_report(
    bot: Bot,
    event: Event,
    matched_groups: tuple[str, ...] = RegexGroup(),
) -> None:
    if not is_private_report_event(event):
        await daily_report.finish()
    if denial := admin_denial(event):
        await daily_report.finish(Message(denial))

    raw_date = matched_groups[0] if len(matched_groups) >= 1 else None
    raw_group_id = matched_groups[1] if len(matched_groups) >= 2 else None
    try:
        target_date = parse_report_date(raw_date)
        group_id = resolve_group_id(event, raw_group_id, "生成日报 昨天 548901561")
    except ValueError as exc:
        await daily_report.finish(Message(str(exc)))

    await daily_report.send(Message(f"开始生成群 {group_id} {target_date.strftime('%Y-%m-%d')} 的日报，稍等一下。"))
    try:
        group_name = await get_group_name(bot, group_id)
        markdown_filename, markdown_content, pdf_filename = await generate_daily_report_files(
            group_id,
            target_date,
            group_name=group_name,
        )
    except Exception as exc:
        logger.exception("Failed to generate daily report")
        await daily_report.finish(Message(f"日报生成失败：{exc}"))

    if not pdf_filename:
        await daily_report.finish(Message("日报生成失败：PDF 未生成，请检查服务器中文字体或 ReportLab 配置。"))

    try:
        await send_existing_file(bot, event, report_file_path(pdf_filename), pdf_filename)
    except Exception:
        logger.exception("Failed to upload daily report")
        await daily_report.finish(Message(f"日报已生成但 PDF 发送失败：{pdf_filename}。请检查 NapCat 是否支持文件上传。"))

    await daily_report.finish(Message(f"日报已生成：{pdf_filename}"))


@daily_report_test_send.handle()
async def handle_daily_report_test_send(
    bot: Bot,
    event: Event,
    matched_groups: tuple[str, ...] = RegexGroup(),
) -> None:
    if not is_private_report_event(event):
        await daily_report_test_send.finish()
    if denial := admin_denial(event):
        await daily_report_test_send.finish(Message(denial))

    raw_date = matched_groups[0] if len(matched_groups) >= 1 else "昨天"
    raw_group_id = matched_groups[1] if len(matched_groups) >= 2 else None
    try:
        target_date = parse_report_date(raw_date)
        group_id = resolve_group_id(event, raw_group_id, "测试日报 昨天 548901561")
    except ValueError as exc:
        await daily_report_test_send.finish(Message(str(exc)))

    await daily_report_test_send.send(Message(f"开始测试发送 {target_date.strftime('%Y-%m-%d')} 的日报给管理员。"))
    try:
        await send_daily_report_to_admins(bot, group_id, target_date)
    except Exception as exc:
        logger.exception("Failed to test send daily report")
        await daily_report_test_send.finish(Message(f"测试发送日报失败：{exc}"))
    await daily_report_test_send.finish(Message("测试日报已发送给管理员。"))
