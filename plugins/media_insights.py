from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from urllib.error import URLError
from urllib.request import Request, urlopen

import aiosqlite
from dotenv import load_dotenv
from nonebot import get_driver, on_fullmatch, on_message
from nonebot.adapters.onebot.v11 import Event, GroupMessageEvent, Message, MessageEvent
from nonebot.log import logger

from plugins.access_control import FEATURE_COLLECTOR, admin_denial, is_group_feature_enabled
from plugins.message_archive import DB_PATH, init_archive_db


load_dotenv(".env.local")

MEDIA_INSIGHTS_ENABLED_ENV = "MEDIA_INSIGHTS_ENABLED"
MEDIA_INSIGHTS_AUTO_ENABLED_ENV = "MEDIA_INSIGHTS_AUTO_ENABLED"
MEDIA_INSIGHTS_BATCH_SIZE_ENV = "MEDIA_INSIGHTS_BATCH_SIZE"
IMAGE_VISION_MODEL_ENV = "IMAGE_VISION_MODEL"
IMAGE_VISION_ENABLED_ENV = "IMAGE_VISION_ENABLED"
IMAGE_VISION_API_KEY_ENV = "IMAGE_VISION_API_KEY"
IMAGE_VISION_BASE_URL_ENV = "IMAGE_VISION_BASE_URL"
IMAGE_VISION_TIMEOUT_SECONDS_ENV = "IMAGE_VISION_TIMEOUT_SECONDS"
DEFAULT_IMAGE_VISION_MODEL = "qwen-vl-plus"
DEFAULT_IMAGE_VISION_TIMEOUT_SECONDS = 45
IMAGE_ANALYSIS_PROMPT = """请识别这张群聊图片，用中文输出，供群聊日报总结使用。

要求：
1. 如果是截图，提取能看清的文字、数字、表格、标题、链接、聊天要点。
2. 如果是照片或表情包，描述画面内容、可能表达的情绪或含义。
3. 如果涉及待办、结论、争议、时间地点人物，请明确列出。
4. 不要编造看不清的内容；看不清就写“看不清”。
5. 输出尽量简洁，但要保留总结需要的关键信息。"""

INSIGHT_IMAGE = "image"
INSIGHT_RECORD = "record"
INSIGHT_FORWARD = "forward"
INSIGHT_JSON = "json"
INSIGHT_XML = "xml"
STATUS_PENDING = "pending"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

driver = get_driver()

media_insight_status = on_fullmatch(("媒体识别状态", "素材识别状态"), priority=5, block=True)
media_insight_scan = on_fullmatch(("扫描媒体识别", "扫描素材识别"), priority=5, block=True)
auto_media_insight = on_message(priority=31, block=False)
auto_process_lock = asyncio.Lock()


def media_insights_enabled() -> bool:
    return os.getenv(MEDIA_INSIGHTS_ENABLED_ENV, "").strip() in {"1", "true", "True", "yes", "on"}


def auto_media_insights_enabled() -> bool:
    return os.getenv(MEDIA_INSIGHTS_AUTO_ENABLED_ENV, "1").strip() in {"1", "true", "True", "yes", "on"}


def batch_size() -> int:
    raw_value = os.getenv(MEDIA_INSIGHTS_BATCH_SIZE_ENV, "30")
    try:
        value = int(raw_value)
    except ValueError:
        return 30
    return min(max(value, 1), 200)


def image_vision_model() -> str:
    return os.getenv(IMAGE_VISION_MODEL_ENV, DEFAULT_IMAGE_VISION_MODEL).strip() or DEFAULT_IMAGE_VISION_MODEL


def image_vision_enabled() -> bool:
    return os.getenv(IMAGE_VISION_ENABLED_ENV, "").strip() in {"1", "true", "True", "yes", "on"}


def image_vision_api_key() -> str | None:
    return os.getenv(IMAGE_VISION_API_KEY_ENV) or os.getenv("OPENAI_API_KEY")


def image_vision_base_url() -> str | None:
    return os.getenv(IMAGE_VISION_BASE_URL_ENV) or os.getenv("OPENAI_BASE_URL")


def image_vision_timeout_seconds() -> int:
    raw_value = os.getenv(IMAGE_VISION_TIMEOUT_SECONDS_ENV, str(DEFAULT_IMAGE_VISION_TIMEOUT_SECONDS))
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_IMAGE_VISION_TIMEOUT_SECONDS
    return max(1, value)


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_segments(raw_message: str) -> list[dict[str, object]]:
    try:
        value = json.loads(raw_message)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def event_segments(event: MessageEvent) -> list[dict[str, object]]:
    segments: list[dict[str, object]] = []
    for segment in event.get_message():
        segments.append({"type": segment.type, "data": dict(segment.data)})
    return segments


def segment_data(segment: dict[str, object]) -> dict[str, object]:
    data = segment.get("data")
    return data if isinstance(data, dict) else {}


def segment_ref(data: dict[str, object]) -> str:
    for key in ("url", "file", "file_id", "id", "data"):
        value = data.get(key)
        if value:
            return str(value)
    return ""


def build_insight(segment: dict[str, object]) -> tuple[str, str, str] | None:
    segment_type = str(segment.get("type", ""))
    data = segment_data(segment)
    ref = segment_ref(data)

    if segment_type == "image":
        return INSIGHT_IMAGE, STATUS_PENDING, f"图片待识别：{ref}"
    if segment_type == "record":
        return INSIGHT_RECORD, STATUS_PENDING, f"语音待转写：{ref}"
    if segment_type == "forward":
        return INSIGHT_FORWARD, STATUS_PENDING, f"聊天记录待展开：{ref}"
    if segment_type == "json":
        return INSIGHT_JSON, STATUS_READY, f"JSON消息：{ref}"
    if segment_type == "xml":
        return INSIGHT_XML, STATUS_READY, f"XML消息：{ref}"
    return None


def build_insights_for_segments(segments: list[dict[str, object]]) -> list[tuple[str, str, str, int, dict[str, object]]]:
    insights: list[tuple[str, str, str, int, dict[str, object]]] = []
    for segment_index, segment in enumerate(segments):
        insight = build_insight(segment)
        if insight is None:
            continue
        insight_type, status, content = insight
        insights.append((insight_type, status, content, segment_index, segment))
    return insights


def has_insight_segments(segments: list[dict[str, object]]) -> bool:
    return any(build_insight(segment) is not None for segment in segments)


def extract_image_url(raw_result: str | None) -> str | None:
    if not raw_result:
        return None
    try:
        value = json.loads(raw_result)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    data = value.get("data")
    if not isinstance(data, dict):
        return None
    for key in ("url", "file", "file_id"):
        image_url = data.get(key)
        if isinstance(image_url, str) and image_url.startswith(("http://", "https://")):
            return image_url
    return None


def check_url_accessible_sync(image_url: str, timeout: int) -> None:
    headers = {"User-Agent": "Mozilla/5.0"}
    request = Request(image_url, method="HEAD", headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status >= 400:
                raise URLError(f"HTTP {response.status}")
            return
    except Exception:
        request = Request(image_url, headers={**headers, "Range": "bytes=0-0"})
        with urlopen(request, timeout=timeout) as response:
            if response.status >= 400:
                raise URLError(f"HTTP {response.status}")
            response.read(1)


async def check_url_accessible(image_url: str) -> None:
    await asyncio.to_thread(check_url_accessible_sync, image_url, min(image_vision_timeout_seconds(), 15))


async def analyze_image_url(image_url: str) -> str:
    api_key = image_vision_api_key()
    if not api_key:
        raise RuntimeError(f"missing {IMAGE_VISION_API_KEY_ENV} or OPENAI_API_KEY")

    from openai import AsyncOpenAI

    base_url = image_vision_base_url()
    client = AsyncOpenAI(api_key=api_key, base_url=base_url) if base_url else AsyncOpenAI(api_key=api_key)
    response = await asyncio.wait_for(
        client.chat.completions.create(
            model=image_vision_model(),
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": IMAGE_ANALYSIS_PROMPT},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
        ),
        timeout=image_vision_timeout_seconds(),
    )
    content = response.choices[0].message.content or ""
    content = content.strip()
    if not content:
        raise RuntimeError("empty image analysis response")
    return content


async def upsert_insight(
    *,
    message_archive_id: int,
    group_id: str,
    insight_type: str,
    segment_index: int = 0,
    insight_key: str = "0",
    status: str,
    content: str,
    raw_result: object,
    error: str | None = None,
    replace_existing: bool = True,
) -> None:
    timestamp = now_text()
    values = (
        message_archive_id,
        group_id,
        insight_type,
        segment_index,
        insight_key,
        status,
        content,
        json.dumps(raw_result, ensure_ascii=False, default=str),
        error,
        timestamp,
        timestamp,
    )
    async with aiosqlite.connect(DB_PATH) as db:
        if replace_existing:
            await db.execute(
                """
                INSERT INTO message_insights (
                    message_archive_id,
                    group_id,
                    insight_type,
                    segment_index,
                    insight_key,
                    status,
                    content,
                    raw_result,
                    error,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_archive_id, insight_type, insight_key) DO UPDATE SET
                    segment_index = excluded.segment_index,
                    status = excluded.status,
                    content = excluded.content,
                    raw_result = excluded.raw_result,
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                values,
            )
        else:
            await db.execute(
                """
                INSERT OR IGNORE INTO message_insights (
                    message_archive_id,
                    group_id,
                    insight_type,
                    segment_index,
                    insight_key,
                    status,
                    content,
                    raw_result,
                    error,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        await db.commit()


async def scan_message_row(row: aiosqlite.Row) -> int:
    insight_count = 0
    segments = load_segments(row["raw_message"])
    for insight_type, status, content, segment_index, segment in build_insights_for_segments(segments):
        await upsert_insight(
            message_archive_id=int(row["id"]),
            group_id=str(row["group_id"]),
            insight_type=insight_type,
            segment_index=segment_index,
            insight_key=str(segment_index),
            status=status,
            content=content,
            raw_result=segment,
            replace_existing=False,
        )
        insight_count += 1
    return insight_count


async def find_collected_message(group_id: str, message_id: str) -> aiosqlite.Row | None:
    if not message_id:
        return None

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, group_id, raw_message
            FROM collected_messages
            WHERE group_id = ? AND message_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (group_id, message_id),
        )
        return await cursor.fetchone()


async def wait_for_collected_message(group_id: str, message_id: str) -> aiosqlite.Row | None:
    for _ in range(10):
        row = await find_collected_message(group_id, message_id)
        if row is not None:
            return row
        await asyncio.sleep(0.2)
    return None


async def auto_process_collected_message(group_id: str, message_id: str) -> None:
    if not media_insights_enabled() or not auto_media_insights_enabled():
        return

    async with auto_process_lock:
        try:
            row = await wait_for_collected_message(group_id, message_id)
            if row is None:
                return
            await scan_message_row(row)
            await process_pending_images(group_id=group_id)
        except Exception:
            logger.exception("Auto media insight failed")


async def scan_recent_messages(limit: int | None = None, group_id: str | None = None) -> tuple[int, int]:
    await init_archive_db()
    scan_limit = limit or batch_size()
    where_clause = "WHERE group_id = ?" if group_id else ""
    params: tuple[object, ...] = (group_id, scan_limit) if group_id else (scan_limit,)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT id, group_id, raw_message
            FROM collected_messages
            {where_clause}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        )
        rows = await cursor.fetchall()

    scanned_count = 0
    insight_count = 0
    for row in rows:
        scanned_count += 1
        insight_count += await scan_message_row(row)

    return scanned_count, insight_count


async def process_pending_images(limit: int | None = None, group_id: str | None = None) -> tuple[int, int, int]:
    await init_archive_db()
    process_limit = limit or batch_size()
    group_filter = "AND group_id = ?" if group_id else ""
    params: tuple[object, ...] = (
        INSIGHT_IMAGE,
        STATUS_PENDING,
        group_id,
        process_limit,
    ) if group_id else (
        INSIGHT_IMAGE,
        STATUS_PENDING,
        process_limit,
    )

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT id, message_archive_id, group_id, raw_result
            , segment_index, insight_key
            FROM message_insights
            WHERE insight_type = ? AND status = ?
            {group_filter}
            ORDER BY id ASC
            LIMIT ?
            """,
            params,
        )
        rows = await cursor.fetchall()

    ready_count = 0
    failed_count = 0
    skipped_count = 0
    for row in rows:
        if not image_vision_enabled():
            skipped_count += 1
            continue

        image_url = extract_image_url(row["raw_result"])
        if not image_url:
            await upsert_insight(
                message_archive_id=int(row["message_archive_id"]),
                group_id=str(row["group_id"]),
                insight_type=INSIGHT_IMAGE,
                segment_index=int(row["segment_index"] or 0),
                insight_key=str(row["insight_key"] or "0"),
                status=STATUS_FAILED,
                content="图片识别失败：没有可访问的图片URL。",
                raw_result={"previous_raw_result": row["raw_result"]},
                error="missing image url",
            )
            failed_count += 1
            continue

        if not image_vision_api_key():
            skipped_count += 1
            continue

        try:
            await check_url_accessible(image_url)
            analysis = await analyze_image_url(image_url)
        except Exception as exc:
            logger.exception("Image insight failed")
            await upsert_insight(
                message_archive_id=int(row["message_archive_id"]),
                group_id=str(row["group_id"]),
                insight_type=INSIGHT_IMAGE,
                segment_index=int(row["segment_index"] or 0),
                insight_key=str(row["insight_key"] or "0"),
                status=STATUS_FAILED,
                content=f"图片识别失败：{exc}",
                raw_result={"image_url": image_url},
                error=str(exc),
            )
            failed_count += 1
            continue

        await upsert_insight(
            message_archive_id=int(row["message_archive_id"]),
            group_id=str(row["group_id"]),
            insight_type=INSIGHT_IMAGE,
            segment_index=int(row["segment_index"] or 0),
            insight_key=str(row["insight_key"] or "0"),
            status=STATUS_READY,
            content=analysis,
            raw_result={"image_url": image_url, "model": image_vision_model()},
        )
        ready_count += 1

    return ready_count, failed_count, skipped_count


async def media_insight_status_text() -> str:
    await init_archive_db()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        total_cursor = await db.execute("SELECT COUNT(*) AS count FROM message_insights")
        total_row = await total_cursor.fetchone()
        by_status_cursor = await db.execute(
            """
            SELECT insight_type, status, COUNT(*) AS count
            FROM message_insights
            GROUP BY insight_type, status
            ORDER BY insight_type, status
            """
        )
        rows = await by_status_cursor.fetchall()

    lines = [
        "媒体识别状态",
        f"实验开关：{'开启' if media_insights_enabled() else '关闭'}",
        f"自动识别：{'开启' if auto_media_insights_enabled() else '关闭'}",
        f"图片调用：{'开启' if image_vision_enabled() else '关闭'}",
        f"图片模型：{image_vision_model()}",
        f"图片密钥：{'已配置' if image_vision_api_key() else '未配置'}",
        f"识别记录：{int(total_row['count'] or 0) if total_row else 0}",
    ]
    for row in rows:
        lines.append(f"{row['insight_type']} / {row['status']}：{row['count']}")
    return "\n".join(lines)


@driver.on_startup
async def startup() -> None:
    if not media_insights_enabled():
        return
    await init_archive_db()
    try:
        await scan_recent_messages()
        if auto_media_insights_enabled():
            asyncio.create_task(process_pending_images())
    except Exception:
        logger.exception("Media insight startup scan failed")


@auto_media_insight.handle()
async def handle_auto_media_insight(event: MessageEvent) -> None:
    if not isinstance(event, GroupMessageEvent):
        return
    if not media_insights_enabled() or not auto_media_insights_enabled():
        return
    if not await is_group_feature_enabled(str(event.group_id), FEATURE_COLLECTOR):
        return

    segments = event_segments(event)
    if not has_insight_segments(segments):
        return

    message_id = str(getattr(event, "message_id", "") or "")
    if not message_id:
        return
    asyncio.create_task(auto_process_collected_message(str(event.group_id), message_id))


@media_insight_status.handle()
async def handle_media_insight_status(event: Event) -> None:
    if denial := admin_denial(event):
        await media_insight_status.finish(Message(denial))
    await media_insight_status.finish(Message(await media_insight_status_text()))


@media_insight_scan.handle()
async def handle_media_insight_scan(event: Event) -> None:
    if denial := admin_denial(event):
        await media_insight_scan.finish(Message(denial))
    if not media_insights_enabled():
        await media_insight_scan.finish(Message(f"媒体识别实验未开启，请设置 {MEDIA_INSIGHTS_ENABLED_ENV}=1。"))
    group_id = str(event.group_id) if isinstance(event, GroupMessageEvent) else None
    scanned_count, insight_count = await scan_recent_messages(group_id=group_id)
    ready_count, failed_count, skipped_count = await process_pending_images(group_id=group_id)
    lines = [
        f"已扫描最近{scanned_count}条消息，生成/更新{insight_count}条识别记录。",
        f"图片识别：成功{ready_count}，失败{failed_count}，跳过{skipped_count}。",
    ]
    if skipped_count:
        lines.append(f"跳过原因通常是未开启 {IMAGE_VISION_ENABLED_ENV}，或未配置 {IMAGE_VISION_API_KEY_ENV} / OPENAI_API_KEY。")
    await media_insight_scan.finish(Message("\n".join(lines)))
