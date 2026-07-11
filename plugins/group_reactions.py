from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import re
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent, MessageSegment
from nonebot.log import logger

from plugins.access_control import (
    FEATURE_BOT_TEASE,
    FEATURE_CONSTANT_RETORT,
    FEATURE_KEYWORD_RETORT,
    admin_user_ids,
    consume_group_feature_usage,
    get_group_feature_limits,
    is_group_feature_enabled,
)
from plugins.ai_chat import ask_ai
from plugins.companion_registry import DB_PATH as COMPANION_DB_PATH, init_companion_db
from plugins.message_archive import message_segments, render_plain_text
from plugins.sensitive_logging import log_fingerprint

DEFAULT_RETORT_IMAGE_PATH = "data/assets/constant_retort_158.jpg"
KEYWORD_RETORT_DB_PATH = Path("data/keyword_retorts.db")
KEYWORD_RETORT_IMAGE_SCAN_ENABLED_ENV = "KEYWORD_RETORT_IMAGE_SCAN_ENABLED"
KEYWORD_RETORT_IMAGE_SCAN_MAX_IMAGES_ENV = "KEYWORD_RETORT_IMAGE_SCAN_MAX_IMAGES"
KEYWORD_RETORT_IMAGE_SCAN_MODEL_ENV = "KEYWORD_RETORT_IMAGE_SCAN_MODEL"
KEYWORD_RETORT_IMAGE_SCAN_API_KEY_ENV = "KEYWORD_RETORT_IMAGE_SCAN_API_KEY"
KEYWORD_RETORT_IMAGE_SCAN_BASE_URL_ENV = "KEYWORD_RETORT_IMAGE_SCAN_BASE_URL"
KEYWORD_RETORT_IMAGE_SCAN_TIMEOUT_ENV = "KEYWORD_RETORT_IMAGE_SCAN_TIMEOUT_SECONDS"
IMAGE_VISION_MODEL_ENV = "IMAGE_VISION_MODEL"
IMAGE_VISION_API_KEY_ENV = "IMAGE_VISION_API_KEY"
IMAGE_VISION_BASE_URL_ENV = "IMAGE_VISION_BASE_URL"
IMAGE_VISION_TIMEOUT_SECONDS_ENV = "IMAGE_VISION_TIMEOUT_SECONDS"
DEFAULT_IMAGE_VISION_MODEL = "qwen3-vl-flash"
DEFAULT_IMAGE_SCAN_TIMEOUT_SECONDS = 12
IMAGE_TEXT_SCAN_PROMPT = (
    "Extract only the visible text in this image. Do not infer from the URL, file name, metadata, "
    "message context, or image description. Return plain text only. If no text is visible, return EMPTY."
)
URL_PATTERN = re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]{1,20}://[^\s<>'\"，。！？、；（）\[\]{}]+"
    r"|\bwww\.[^\s<>'\"，。！？、；（）\[\]{}]+"
    r"|\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,}(?:/[^\s<>'\"，。！？、；（）\[\]{}]*)?"
)
LINK_ADDRESS_FIELD_NAMES = {
    "url",
    "uri",
    "href",
    "link",
    "jumpurl",
    "jump_url",
    "targeturl",
    "target_url",
    "sourceurl",
    "source_url",
    "previewurl",
    "preview_url",
    "file",
    "fileid",
    "file_id",
    "src",
    "icon",
    "image",
    "thumbnail",
    "thumb",
    "cover",
}
TEXT_FIELD_NAMES = {
    "text",
    "summary",
    "name",
    "title",
    "prompt",
    "desc",
    "description",
    "content",
    "brief",
    "nickname",
    "address",
    "label",
}
MAX_BOT_TEASE_CONTEXT_CHARS = 500
MAX_KEYWORD_RETORT_RULES = 50
MAX_KEYWORD_RETORT_REPLIES = 30
TEXT_REPLY_TYPE = "text"
IMAGE_REPLY_TYPE = "image"
_keyword_retort_db_ready = False
BOT_TEASE_FALLBACKS = (
    "你这个关键词一响，猎宝耳朵都竖起来了。",
    "收到，bot 同行今日也在努力营业。",
    "这话题我先接一小口，别把群聊带跑太远。",
)

group_reactions = on_message(priority=29, block=False)


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def normalize_text(text: str | None) -> str:
    return " ".join(str(text or "").split()).strip()


def strip_urls(text: str) -> str:
    return URL_PATTERN.sub(" ", str(text or "").replace("\\/", "/"))


def normalized_field_name(key: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key or "").lower())


def is_link_address_field(key: object) -> bool:
    normalized = normalized_field_name(key)
    return normalized in {normalized_field_name(name) for name in LINK_ADDRESS_FIELD_NAMES} or normalized.endswith(("url", "uri"))


def is_text_field(key: object) -> bool:
    return normalized_field_name(key) in {normalized_field_name(name) for name in TEXT_FIELD_NAMES}


def safe_payload_text(value: object, parent_key: object = "") -> str:
    if is_link_address_field(parent_key):
        return ""
    if value is None:
        return ""
    if isinstance(value, dict):
        parts = []
        for key, child in value.items():
            if is_link_address_field(key):
                continue
            if is_text_field(key) or isinstance(child, (dict, list)):
                parts.append(safe_payload_text(child, key))
        return normalize_text(" ".join(parts))
    if isinstance(value, list):
        return normalize_text(" ".join(safe_payload_text(item, parent_key) for item in value))
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("{", "[")):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if parsed is not None and not isinstance(parsed, str):
                return safe_payload_text(parsed, parent_key)
        return normalize_text(strip_urls(text))
    return normalize_text(strip_urls(str(value)))


def append_safe_text(text_parts: list[str], value: object, key: object = "") -> None:
    text = safe_payload_text(value, key)
    if text:
        text_parts.append(text)


def message_text_for_keyword_retort(event: MessageEvent) -> str:
    text_parts: list[str] = [event.get_plaintext()]
    for segment in message_segments(event):
        segment_type = str(segment.get("type") or "")
        data = segment.get("data")
        if not isinstance(data, dict):
            continue
        if segment_type in {"image", "mface"}:
            for key in ("summary", "name", "title", "prompt"):
                append_safe_text(text_parts, data.get(key), key)
            continue
        for key in ("text", "summary", "name", "title", "prompt", "desc", "description", "content", "data"):
            append_safe_text(text_parts, data.get(key), key)
    return normalize_text(strip_urls(" ".join(text_parts)))


def keyword_retort_image_scan_enabled() -> bool:
    return os.getenv(KEYWORD_RETORT_IMAGE_SCAN_ENABLED_ENV, "1").strip() not in {"0", "false", "False", "off", "no"}


def int_env(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError:
        value = default
    value = max(value, minimum)
    if maximum is not None:
        value = min(value, maximum)
    return value


def image_scan_max_images() -> int:
    return int_env(KEYWORD_RETORT_IMAGE_SCAN_MAX_IMAGES_ENV, 2, minimum=1, maximum=5)


def image_scan_timeout_seconds() -> int:
    return int_env(
        KEYWORD_RETORT_IMAGE_SCAN_TIMEOUT_ENV,
        min(int_env(IMAGE_VISION_TIMEOUT_SECONDS_ENV, DEFAULT_IMAGE_SCAN_TIMEOUT_SECONDS), DEFAULT_IMAGE_SCAN_TIMEOUT_SECONDS),
        minimum=3,
        maximum=30,
    )


def keyword_retort_image_model() -> str:
    return (
        os.getenv(KEYWORD_RETORT_IMAGE_SCAN_MODEL_ENV)
        or os.getenv(IMAGE_VISION_MODEL_ENV)
        or DEFAULT_IMAGE_VISION_MODEL
    ).strip() or DEFAULT_IMAGE_VISION_MODEL


def keyword_retort_image_api_key() -> str | None:
    return os.getenv(KEYWORD_RETORT_IMAGE_SCAN_API_KEY_ENV) or os.getenv(IMAGE_VISION_API_KEY_ENV) or os.getenv("OPENAI_API_KEY")


def keyword_retort_image_base_url() -> str | None:
    return os.getenv(KEYWORD_RETORT_IMAGE_SCAN_BASE_URL_ENV) or os.getenv(IMAGE_VISION_BASE_URL_ENV) or os.getenv("OPENAI_BASE_URL")


def image_urls_from_event(event: MessageEvent) -> list[str]:
    urls: list[str] = []
    for segment in message_segments(event):
        if segment.get("type") not in {"image", "mface"}:
            continue
        data = segment.get("data")
        if not isinstance(data, dict):
            continue
        for key in ("url", "file", "file_id"):
            value = data.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                urls.append(value)
                break
    return urls[: image_scan_max_images()]


async def extract_image_text(image_url: str) -> str:
    api_key = keyword_retort_image_api_key()
    if not api_key:
        logger.info("Keyword retort image OCR skipped: missing image vision api key")
        return ""

    from openai import AsyncOpenAI

    base_url = keyword_retort_image_base_url()
    client = AsyncOpenAI(api_key=api_key, base_url=base_url) if base_url else AsyncOpenAI(api_key=api_key)
    response = await asyncio.wait_for(
        client.chat.completions.create(
            model=keyword_retort_image_model(),
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": IMAGE_TEXT_SCAN_PROMPT,
                        },
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
            temperature=0,
            max_tokens=256,
        ),
        timeout=image_scan_timeout_seconds(),
    )
    text = normalize_text(strip_urls(response.choices[0].message.content or ""))
    return "" if text.upper() == "EMPTY" else text


async def keyword_retort_enabled(event: MessageEvent) -> bool:
    if isinstance(event, GroupMessageEvent):
        group_id = str(event.group_id)
        return (
            await is_group_feature_enabled(group_id, FEATURE_KEYWORD_RETORT)
            or await is_group_feature_enabled(group_id, FEATURE_CONSTANT_RETORT)
        )
    return str(event.user_id) in admin_user_ids()


def parse_keywords(value: object) -> list[str]:
    if isinstance(value, list):
        return [normalize_text(str(item)) for item in value if normalize_text(str(item))]
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        return [normalize_text(str(item)) for item in parsed if normalize_text(str(item))]
    return [normalize_text(part) for part in re.split(r"[,，、\s]+", str(value or "")) if normalize_text(part)]


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def init_keyword_retort_db() -> None:
    global _keyword_retort_db_ready
    KEYWORD_RETORT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(KEYWORD_RETORT_DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS keyword_retort_group_state (
                group_id TEXT PRIMARY KEY,
                default_seeded INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS keyword_retort_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                keyword TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                image_match_enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_keyword_retort_rules_group
            ON keyword_retort_rules (group_id, enabled, updated_at)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS keyword_retort_replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id INTEGER NOT NULL,
                reply_type TEXT NOT NULL DEFAULT 'text',
                content TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_keyword_retort_replies_rule
            ON keyword_retort_replies (rule_id, enabled)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS keyword_retort_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                rule_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_keyword_retort_usage_rule_time
            ON keyword_retort_usage (group_id, rule_id, created_at)
            """
        )
        await db.commit()
    _keyword_retort_db_ready = True


async def ensure_keyword_retort_db() -> None:
    if not _keyword_retort_db_ready:
        await init_keyword_retort_db()


def normalize_reply_type(value: object) -> str:
    reply_type = str(value or TEXT_REPLY_TYPE).strip().lower()
    return IMAGE_REPLY_TYPE if reply_type == IMAGE_REPLY_TYPE else TEXT_REPLY_TYPE


def normalize_keyword_retort_reply(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        content = normalize_text(value.get("content"))
        reply_type = normalize_reply_type(value.get("reply_type") or value.get("type"))
        enabled = bool(value.get("enabled", True))
    else:
        content = normalize_text(value)
        reply_type = TEXT_REPLY_TYPE
        enabled = True
    if not content:
        return None
    return {
        "reply_type": reply_type,
        "content": content[:1000],
        "enabled": enabled,
    }


def normalize_keyword_retort_rule(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    keyword = normalize_text(value.get("keyword"))[:80]
    if not keyword:
        return None
    replies_value = value.get("replies")
    replies = replies_value if isinstance(replies_value, list) else []
    normalized_replies: list[dict[str, object]] = []
    for reply in replies:
        normalized_reply = normalize_keyword_retort_reply(reply)
        if normalized_reply:
            normalized_replies.append(normalized_reply)
        if len(normalized_replies) >= MAX_KEYWORD_RETORT_REPLIES:
            break
    try:
        rule_id = int(value.get("id") or 0)
    except (TypeError, ValueError):
        rule_id = 0
    return {
        "id": rule_id,
        "keyword": keyword,
        "enabled": bool(value.get("enabled", True)),
        "image_match_enabled": bool(value.get("image_match_enabled", True)),
        "replies": normalized_replies,
    }


async def ensure_default_keyword_retort_rule(group_id: str) -> None:
    await ensure_keyword_retort_db()
    timestamp = now_text()
    async with aiosqlite.connect(KEYWORD_RETORT_DB_PATH) as db:
        cursor = await db.execute(
            "SELECT default_seeded FROM keyword_retort_group_state WHERE group_id = ?",
            (group_id,),
        )
        state_row = await cursor.fetchone()
        if state_row is not None:
            return

        count_cursor = await db.execute(
            "SELECT COUNT(*) FROM keyword_retort_rules WHERE group_id = ?",
            (group_id,),
        )
        count_row = await count_cursor.fetchone()
        if not int((count_row or [0])[0] or 0):
            cursor = await db.execute(
                """
                INSERT INTO keyword_retort_rules (
                    group_id, keyword, enabled, image_match_enabled, created_at, updated_at
                )
                VALUES (?, '158', 1, 1, ?, ?)
                """,
                (group_id, timestamp, timestamp),
            )
            rule_id = int(cursor.lastrowid)
            await db.execute(
                """
                INSERT INTO keyword_retort_replies (
                    rule_id, reply_type, content, enabled, created_at, updated_at
                )
                VALUES (?, ?, ?, 1, ?, ?)
                """,
                (rule_id, IMAGE_REPLY_TYPE, os.getenv("CONSTANT_RETORT_IMAGE_PATH", DEFAULT_RETORT_IMAGE_PATH), timestamp, timestamp),
            )
        await db.execute(
            """
            INSERT INTO keyword_retort_group_state (group_id, default_seeded, updated_at)
            VALUES (?, 1, ?)
            """,
            (group_id, timestamp),
        )
        await db.commit()


async def keyword_retort_rule_usage(group_id: str, rule_id: int) -> dict[str, int]:
    await ensure_keyword_retort_db()
    now = datetime.now()
    thresholds = {
        "per_minute": (now - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"),
        "per_hour": (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
        "per_day": (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
    }
    usage: dict[str, int] = {}
    async with aiosqlite.connect(KEYWORD_RETORT_DB_PATH) as db:
        await db.execute(
            """
            DELETE FROM keyword_retort_usage
            WHERE group_id = ? AND rule_id = ? AND created_at < ?
            """,
            (group_id, rule_id, thresholds["per_day"]),
        )
        for key, threshold in thresholds.items():
            cursor = await db.execute(
                """
                SELECT COUNT(*)
                FROM keyword_retort_usage
                WHERE group_id = ? AND rule_id = ? AND created_at >= ?
                """,
                (group_id, rule_id, threshold),
            )
            row = await cursor.fetchone()
            usage[key] = int(row[0] or 0) if row else 0
        await db.commit()
    return usage


async def consume_keyword_retort_rule_usage(group_id: str, rule_id: int) -> bool:
    await ensure_keyword_retort_db()
    limits = await get_group_feature_limits(group_id, FEATURE_KEYWORD_RETORT)
    if any(limits[key] <= 0 for key in ("per_minute", "per_hour", "per_day")):
        return False
    usage = await keyword_retort_rule_usage(group_id, rule_id)
    if any(usage[key] >= limits[key] for key in ("per_minute", "per_hour", "per_day")):
        return False
    async with aiosqlite.connect(KEYWORD_RETORT_DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO keyword_retort_usage (group_id, rule_id, created_at)
            VALUES (?, ?, ?)
            """,
            (group_id, rule_id, now_text()),
        )
        await db.commit()
    return True


async def list_keyword_retort_rules(group_id: str, *, include_usage: bool = True) -> list[dict[str, object]]:
    await ensure_default_keyword_retort_rule(group_id)
    async with aiosqlite.connect(KEYWORD_RETORT_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, group_id, keyword, enabled, image_match_enabled, created_at, updated_at
            FROM keyword_retort_rules
            WHERE group_id = ?
            ORDER BY id ASC
            """,
            (group_id,),
        )
        rule_rows = await cursor.fetchall()
        rule_ids = [int(row["id"]) for row in rule_rows]
        replies_by_rule_id: dict[int, list[dict[str, object]]] = {rule_id: [] for rule_id in rule_ids}
        if rule_ids:
            placeholders = ", ".join("?" for _ in rule_ids)
            reply_cursor = await db.execute(
                f"""
                SELECT id, rule_id, reply_type, content, enabled, created_at, updated_at
                FROM keyword_retort_replies
                WHERE rule_id IN ({placeholders})
                ORDER BY id ASC
                """,
                tuple(rule_ids),
            )
            for row in await reply_cursor.fetchall():
                replies_by_rule_id.setdefault(int(row["rule_id"]), []).append(
                    {
                        "id": int(row["id"]),
                        "reply_type": str(row["reply_type"] or TEXT_REPLY_TYPE),
                        "content": str(row["content"] or ""),
                        "enabled": bool(row["enabled"]),
                        "created_at": str(row["created_at"] or ""),
                        "updated_at": str(row["updated_at"] or ""),
                    }
                )

    rules: list[dict[str, object]] = []
    for row in rule_rows:
        rule_id = int(row["id"])
        item: dict[str, object] = {
            "id": rule_id,
            "group_id": str(row["group_id"] or ""),
            "keyword": str(row["keyword"] or ""),
            "enabled": bool(row["enabled"]),
            "image_match_enabled": bool(row["image_match_enabled"]),
            "replies": replies_by_rule_id.get(rule_id, []),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }
        if include_usage:
            item["usage"] = await keyword_retort_rule_usage(group_id, rule_id)
        rules.append(item)
    return rules


async def keyword_retort_state(group_id: str) -> dict[str, object]:
    return {
        "rules": await list_keyword_retort_rules(group_id),
        "image_scan": {
            "enabled": keyword_retort_image_scan_enabled(),
            "model": keyword_retort_image_model(),
            "max_images": image_scan_max_images(),
        },
    }


async def save_keyword_retort_rules(group_id: str, raw_rules: object) -> dict[str, object]:
    await ensure_keyword_retort_db()
    source_rules = raw_rules if isinstance(raw_rules, list) else []
    normalized_rules: list[dict[str, object]] = []
    seen_keywords: set[str] = set()
    for raw_rule in source_rules:
        rule = normalize_keyword_retort_rule(raw_rule)
        if not rule:
            continue
        keyword_key = str(rule["keyword"]).lower()
        if keyword_key in seen_keywords:
            continue
        seen_keywords.add(keyword_key)
        normalized_rules.append(rule)
        if len(normalized_rules) >= MAX_KEYWORD_RETORT_RULES:
            break

    timestamp = now_text()
    async with aiosqlite.connect(KEYWORD_RETORT_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        existing_cursor = await db.execute(
            "SELECT id FROM keyword_retort_rules WHERE group_id = ?",
            (group_id,),
        )
        existing_ids = {int(row["id"]) for row in await existing_cursor.fetchall()}
        kept_ids: set[int] = set()

        for rule in normalized_rules:
            rule_id = int(rule.get("id") or 0)
            if rule_id in existing_ids:
                await db.execute(
                    """
                    UPDATE keyword_retort_rules
                    SET keyword = ?,
                        enabled = ?,
                        image_match_enabled = ?,
                        updated_at = ?
                    WHERE id = ? AND group_id = ?
                    """,
                    (
                        rule["keyword"],
                        1 if rule["enabled"] else 0,
                        1 if rule["image_match_enabled"] else 0,
                        timestamp,
                        rule_id,
                        group_id,
                    ),
                )
            else:
                cursor = await db.execute(
                    """
                    INSERT INTO keyword_retort_rules (
                        group_id, keyword, enabled, image_match_enabled, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        group_id,
                        rule["keyword"],
                        1 if rule["enabled"] else 0,
                        1 if rule["image_match_enabled"] else 0,
                        timestamp,
                        timestamp,
                    ),
                )
                rule_id = int(cursor.lastrowid)
            kept_ids.add(rule_id)
            await db.execute("DELETE FROM keyword_retort_replies WHERE rule_id = ?", (rule_id,))
            for reply in list(rule.get("replies") or [])[:MAX_KEYWORD_RETORT_REPLIES]:
                await db.execute(
                    """
                    INSERT INTO keyword_retort_replies (
                        rule_id, reply_type, content, enabled, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rule_id,
                        reply["reply_type"],
                        reply["content"],
                        1 if reply["enabled"] else 0,
                        timestamp,
                        timestamp,
                    ),
                )

        delete_ids = sorted(existing_ids - kept_ids)
        if delete_ids:
            placeholders = ", ".join("?" for _ in delete_ids)
            await db.execute(f"DELETE FROM keyword_retort_replies WHERE rule_id IN ({placeholders})", tuple(delete_ids))
            await db.execute(f"DELETE FROM keyword_retort_usage WHERE rule_id IN ({placeholders})", tuple(delete_ids))
            await db.execute(
                f"DELETE FROM keyword_retort_rules WHERE group_id = ? AND id IN ({placeholders})",
                (group_id, *delete_ids),
            )
        await db.execute(
            """
            INSERT INTO keyword_retort_group_state (group_id, default_seeded, updated_at)
            VALUES (?, 1, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                default_seeded = 1,
                updated_at = excluded.updated_at
            """,
            (group_id, timestamp),
        )
        await db.commit()
    return await keyword_retort_state(group_id)


async def bot_target(group_id: str, user_id: str) -> dict[str, object] | None:
    await init_companion_db()
    async with aiosqlite.connect(COMPANION_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT display_name, is_bot, bot_keywords
            FROM companion_targets
            WHERE group_id = ? AND user_id = ?
            """,
            (group_id, user_id),
        )
        row = await cursor.fetchone()
    if row is None or not bool(row["is_bot"]):
        return None
    return {
        "display_name": str(row["display_name"] or "").strip(),
        "bot_keywords": parse_keywords(row["bot_keywords"]),
    }


def keyword_matched(message_text: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    normalized = message_text.lower()
    return any(keyword.lower() in normalized for keyword in keywords)


async def build_bot_tease_reply(target: dict[str, object], message_text: str) -> str:
    name = str(target.get("display_name") or "这个bot").strip()
    prompt = (
        "你是猎bot，在群聊里轻轻逗一下另一个 bot。"
        "只输出一句中文，20字以内，语气轻松，不攻击、不阴阳怪气、不刷屏。"
        "可以承接对方刚才的话，也可以顺势抛一个小梗。\n\n"
        f"对方bot：{name}\n"
        f"对方刚说：{message_text[:MAX_BOT_TEASE_CONTEXT_CHARS]}"
    )
    try:
        reply = await ask_ai(prompt)
    except Exception:
        logger.exception("Bot tease AI generation failed")
        return random.choice(BOT_TEASE_FALLBACKS)
    reply = normalize_text(reply).strip("「」")
    if not reply or "AI没有返回内容" in reply:
        return random.choice(BOT_TEASE_FALLBACKS)
    return reply[:80]


def enabled_rule_replies(rule: dict[str, object]) -> list[dict[str, object]]:
    replies = rule.get("replies")
    if not isinstance(replies, list):
        return []
    return [
        reply
        for reply in replies
        if isinstance(reply, dict) and reply.get("enabled", True) and normalize_text(reply.get("content"))
    ]


def rule_matches_text(rule: dict[str, object], text: str) -> bool:
    keyword = normalize_text(rule.get("keyword")).lower()
    if not keyword:
        return False
    return keyword in text.lower()


def sorted_matching_rules(rules: list[dict[str, object]], text: str) -> list[dict[str, object]]:
    matched = [rule for rule in rules if rule_matches_text(rule, text)]
    matched.sort(key=lambda item: len(str(item.get("keyword") or "")), reverse=True)
    return matched


async def matching_keyword_retort_rule(group_id: str, event: MessageEvent) -> dict[str, object] | None:
    rules = [
        rule
        for rule in await list_keyword_retort_rules(group_id, include_usage=False)
        if rule.get("enabled") and enabled_rule_replies(rule)
    ]
    if not rules:
        return None

    message_text = message_text_for_keyword_retort(event)
    text_matches = sorted_matching_rules(rules, message_text)
    if text_matches:
        return text_matches[0]

    image_rules = [rule for rule in rules if rule.get("image_match_enabled", True)]
    if not image_rules or not keyword_retort_image_scan_enabled():
        return None

    for image_url in image_urls_from_event(event):
        try:
            image_text = await extract_image_text(image_url)
        except Exception:
            logger.exception("Keyword retort image OCR failed")
            continue
        image_matches = sorted_matching_rules(image_rules, image_text)
        if image_matches:
            logger.info(
                f"Keyword retort image OCR matched: scope_fingerprint={log_fingerprint('group_id', group_id)}, "
                f"rule_id={image_matches[0].get('id')}, "
                f"keyword_fingerprint={log_fingerprint('keyword', image_matches[0].get('keyword'))}"
            )
            return image_matches[0]
    return None


def image_segment_from_reply_content(content: str) -> MessageSegment | None:
    value = normalize_text(content)
    if value.startswith(("http://", "https://", "base64://")):
        return MessageSegment.image(value)

    image_path = Path(value)
    if not image_path.is_absolute():
        image_path = project_root() / image_path
    if not image_path.exists() or not image_path.is_file():
        logger.warning(
            "Keyword retort image reply is missing: path_fingerprint=%s",
            log_fingerprint("image_path", image_path),
        )
        return None
    image_data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return MessageSegment.image(f"base64://{image_data}")


def message_from_reply(reply: dict[str, object]) -> Message | None:
    content = normalize_text(reply.get("content"))
    if not content:
        return None
    reply_type = normalize_reply_type(reply.get("reply_type"))
    if reply_type == IMAGE_REPLY_TYPE:
        segment = image_segment_from_reply_content(content)
        return Message(segment) if segment is not None else None
    return Message(content)


async def send_keyword_retort(bot: Bot, event: MessageEvent, rule: dict[str, object]) -> bool:
    replies = enabled_rule_replies(rule)
    random.shuffle(replies)
    for reply in replies:
        message = message_from_reply(reply)
        if message is not None:
            await bot.send(event, message)
            return True
    return False


async def maybe_keyword_retort(bot: Bot, event: MessageEvent) -> bool:
    if not isinstance(event, GroupMessageEvent):
        return False
    if str(getattr(event, "user_id", "")) == str(bot.self_id):
        return False
    group_id = str(event.group_id)
    if not await keyword_retort_enabled(event):
        return False

    rule = await matching_keyword_retort_rule(group_id, event)
    if rule is None:
        return False

    rule_id = int(rule.get("id") or 0)
    if rule_id <= 0:
        return False
    if not await consume_keyword_retort_rule_usage(group_id, rule_id):
        logger.info(
            f"Keyword retort skipped by rule rate limit: "
            f"scope_fingerprint={log_fingerprint('group_id', group_id)}, rule_id={rule_id}"
        )
        return False
    if not await send_keyword_retort(bot, event, rule):
        return False
    logger.info(
        f"Keyword retort sent: scope_fingerprint={log_fingerprint('group_id', group_id)}, "
        f"rule_id={rule_id}, keyword_fingerprint={log_fingerprint('keyword', rule.get('keyword'))}"
    )
    return True


async def maybe_bot_tease(bot: Bot, event: GroupMessageEvent) -> bool:
    group_id = str(event.group_id)
    if not await is_group_feature_enabled(group_id, FEATURE_BOT_TEASE):
        return False
    if str(event.user_id) == str(bot.self_id):
        return False

    target = await bot_target(group_id, str(event.user_id))
    if target is None:
        return False

    message_text = normalize_text(render_plain_text(message_segments(event)) or event.get_plaintext())
    if not keyword_matched(message_text, list(target.get("bot_keywords") or [])):
        return False
    if not await consume_group_feature_usage(group_id, FEATURE_BOT_TEASE):
        return False

    reply = await build_bot_tease_reply(target, message_text)
    await bot.send(event, Message(reply))
    return True


@group_reactions.handle()
async def handle_group_reactions(bot: Bot, event: MessageEvent) -> None:
    try:
        if await maybe_keyword_retort(bot, event):
            return
        if isinstance(event, GroupMessageEvent):
            await maybe_bot_tease(bot, event)
    except Exception:
        logger.exception("Group reaction handler failed")
