from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import re
from pathlib import Path

import aiosqlite
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent, MessageSegment
from nonebot.log import logger

from plugins.access_control import (
    FEATURE_BOT_TEASE,
    FEATURE_CONSTANT_RETORT,
    admin_user_ids,
    consume_group_feature_usage,
    is_group_feature_enabled,
)
from plugins.ai_chat import ask_ai
from plugins.companion_registry import DB_PATH as COMPANION_DB_PATH, init_companion_db
from plugins.message_archive import message_segments, render_plain_text


DEFAULT_RETORT_IMAGE_PATH = "data/assets/constant_retort_158.jpg"
CONSTANT_RETORT_IMAGE_SCAN_ENABLED_ENV = "CONSTANT_RETORT_IMAGE_SCAN_ENABLED"
CONSTANT_RETORT_IMAGE_SCAN_MAX_IMAGES_ENV = "CONSTANT_RETORT_IMAGE_SCAN_MAX_IMAGES"
CONSTANT_RETORT_IMAGE_SCAN_TIMEOUT_ENV = "CONSTANT_RETORT_IMAGE_SCAN_TIMEOUT_SECONDS"
IMAGE_VISION_MODEL_ENV = "IMAGE_VISION_MODEL"
IMAGE_VISION_API_KEY_ENV = "IMAGE_VISION_API_KEY"
IMAGE_VISION_BASE_URL_ENV = "IMAGE_VISION_BASE_URL"
IMAGE_VISION_TIMEOUT_SECONDS_ENV = "IMAGE_VISION_TIMEOUT_SECONDS"
DEFAULT_IMAGE_VISION_MODEL = "qwen-vl-plus"
DEFAULT_IMAGE_SCAN_TIMEOUT_SECONDS = 12
IMAGE_SCAN_PROMPT = (
    "Look at the image only. Is the Arabic numeral string 158 visibly present in the image? "
    "Chinese words such as 一五八 do not count. Reply with exactly YES or NO."
)
MAX_BOT_TEASE_CONTEXT_CHARS = 500
BOT_TEASE_FALLBACKS = (
    "你这个关键词一响，猎宝耳朵都竖起来了。",
    "收到，bot 同行今日也在努力营业。",
    "这话题我先接一小口，别把群聊带跑太远。",
)

group_reactions = on_message(priority=29, block=False)


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def retort_image_path() -> Path:
    raw_path = os.getenv("CONSTANT_RETORT_IMAGE_PATH", DEFAULT_RETORT_IMAGE_PATH)
    path = Path(raw_path)
    if not path.is_absolute():
        path = project_root() / path
    return path


def normalize_text(text: str | None) -> str:
    return " ".join(str(text or "").split()).strip()


def contains_158(event: MessageEvent) -> bool:
    text_parts: list[str] = [event.get_plaintext(), render_plain_text(message_segments(event))]
    for segment in message_segments(event):
        data = segment.get("data")
        if isinstance(data, dict):
            for key in ("text", "summary", "name", "title", "prompt", "data"):
                value = data.get(key)
                if value:
                    text_parts.append(str(value))
    return any("158" in part for part in text_parts)


def constant_retort_image_scan_enabled() -> bool:
    return os.getenv(CONSTANT_RETORT_IMAGE_SCAN_ENABLED_ENV, "1").strip() not in {"0", "false", "False", "off", "no"}


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
    return int_env(CONSTANT_RETORT_IMAGE_SCAN_MAX_IMAGES_ENV, 2, minimum=1, maximum=5)


def image_scan_timeout_seconds() -> int:
    return int_env(
        CONSTANT_RETORT_IMAGE_SCAN_TIMEOUT_ENV,
        min(int_env(IMAGE_VISION_TIMEOUT_SECONDS_ENV, DEFAULT_IMAGE_SCAN_TIMEOUT_SECONDS), DEFAULT_IMAGE_SCAN_TIMEOUT_SECONDS),
        minimum=3,
        maximum=30,
    )


def image_vision_model() -> str:
    return os.getenv(IMAGE_VISION_MODEL_ENV, DEFAULT_IMAGE_VISION_MODEL).strip() or DEFAULT_IMAGE_VISION_MODEL


def image_vision_api_key() -> str | None:
    return os.getenv(IMAGE_VISION_API_KEY_ENV) or os.getenv("OPENAI_API_KEY")


def image_vision_base_url() -> str | None:
    return os.getenv(IMAGE_VISION_BASE_URL_ENV) or os.getenv("OPENAI_BASE_URL")


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


async def image_url_contains_158(image_url: str) -> bool:
    api_key = image_vision_api_key()
    if not api_key:
        logger.info("Constant retort image scan skipped: missing image vision api key")
        return False

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
                        {
                            "type": "text",
                            "text": IMAGE_SCAN_PROMPT,
                        },
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
            temperature=0,
            max_tokens=3,
        ),
        timeout=image_scan_timeout_seconds(),
    )
    content = re.sub(r"[^A-Z]", "", (response.choices[0].message.content or "").upper())
    return content.startswith("YES")


async def event_image_contains_158(event: MessageEvent) -> bool:
    if not constant_retort_image_scan_enabled():
        return False
    urls = image_urls_from_event(event)
    if not urls:
        return False
    for image_url in urls:
        try:
            if await image_url_contains_158(image_url):
                logger.info(f"Constant retort image scan matched: scope={retort_scope(event)}")
                return True
        except Exception:
            logger.exception("Constant retort image scan failed")
    return False


async def event_contains_158(event: MessageEvent) -> bool:
    return contains_158(event) or await event_image_contains_158(event)


def retort_scope(event: MessageEvent) -> str:
    if isinstance(event, GroupMessageEvent):
        return str(event.group_id)
    return f"private:{event.user_id}"


async def constant_retort_enabled(event: MessageEvent) -> bool:
    if isinstance(event, GroupMessageEvent):
        return await is_group_feature_enabled(str(event.group_id), FEATURE_CONSTANT_RETORT)
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


def retort_image_segment() -> MessageSegment | None:
    image_path = retort_image_path()
    if not image_path.exists():
        logger.warning(f"Constant retort image is missing: {image_path}")
        return None
    image_data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return MessageSegment.image(f"base64://{image_data}")


async def send_constant_retort(bot: Bot, event: MessageEvent) -> None:
    segment = retort_image_segment()
    if segment is None:
        return
    await bot.send(event, Message(segment))


async def maybe_constant_retort(bot: Bot, event: MessageEvent) -> bool:
    if str(getattr(event, "user_id", "")) == str(bot.self_id):
        return False
    if not await constant_retort_enabled(event):
        if contains_158(event):
            logger.info(f"Constant retort ignored because feature is disabled: scope={retort_scope(event)}")
        return False
    if not await event_contains_158(event):
        return False
    if not await consume_group_feature_usage(retort_scope(event), FEATURE_CONSTANT_RETORT):
        logger.info(f"Constant retort skipped by rate limit: scope={retort_scope(event)}")
        return False
    await send_constant_retort(bot, event)
    logger.info(f"Constant retort sent: scope={retort_scope(event)}")
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
        if await maybe_constant_retort(bot, event):
            return
        if isinstance(event, GroupMessageEvent):
            await maybe_bot_tease(bot, event)
    except Exception:
        logger.exception("Group reaction handler failed")
