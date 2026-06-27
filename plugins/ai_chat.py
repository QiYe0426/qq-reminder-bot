from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv
from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent
from nonebot.log import logger
from openai import AsyncOpenAI

from plugins.access_control import FEATURE_COLLECTOR, is_group_feature_enabled
from plugins.message_archive import save_ai_reply


load_dotenv(".env.local")

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_QUESTION_LENGTH = 800
DEFAULT_MAX_REPLY_LENGTH = 1200
AI_INSTRUCTIONS = (
    "你是猎bot接入的AI助手。"
    "用简洁、自然的中文回答。"
    "如果用户在学习代码，优先用新手能理解的方式解释。"
)
PRIVATE_PREFIXES = ("猎宝，", "猎宝 ")

ai_chat = on_message(priority=20, block=False)


def get_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return max(1, value)


def group_mentions_bot(event: GroupMessageEvent, bot: Bot) -> bool:
    for segment in event.get_message():
        if segment.type == "at" and str(segment.data.get("qq")) == str(bot.self_id):
            return True

    raw_message = str(getattr(event, "raw_message", "")) or str(event.get_message())
    return (
        f"[CQ:at,qq={bot.self_id}]" in raw_message
        or f"[at:qq={bot.self_id}]" in raw_message
    )


def strip_ai_prefix(text: str) -> str | None:
    for prefix in PRIVATE_PREFIXES:
        if text.startswith(prefix):
            return text.removeprefix(prefix).strip()
    return None


def extract_question(event: MessageEvent) -> str:
    return event.get_plaintext().strip()


def shorten_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n......"


async def ask_ai(question: str) -> str:
    api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        return "AI还没有配置DEEPSEEK_API_KEY。"

    model = os.getenv("AI_MODEL", DEFAULT_MODEL)
    base_url = os.getenv("DEEPSEEK_BASE_URL") or os.getenv("OPENAI_BASE_URL") or DEFAULT_BASE_URL
    timeout_seconds = get_int_env("AI_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
    max_reply_length = get_int_env("AI_MAX_REPLY_LENGTH", DEFAULT_MAX_REPLY_LENGTH)

    client = AsyncOpenAI(api_key=api_key, base_url=base_url) if base_url else AsyncOpenAI(api_key=api_key)
    response = await asyncio.wait_for(
        client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": AI_INSTRUCTIONS},
                {"role": "user", "content": question},
            ],
        ),
        timeout=timeout_seconds,
    )
    answer = (response.choices[0].message.content or "").strip()
    if not answer:
        return "AI没有返回内容。"
    return shorten_text(answer, max_reply_length)


@ai_chat.handle()
async def handle_ai_chat(bot: Bot, event: MessageEvent) -> None:
    question = extract_question(event)
    if isinstance(event, GroupMessageEvent):
        if group_mentions_bot(event, bot):
            pass
        else:
            triggered_question = strip_ai_prefix(question)
            if triggered_question is None:
                return
            question = triggered_question
        if not question:
            await ai_chat.finish(Message("你叫我啦？把问题写在后面就行。"))
    else:
        prefixed_question = strip_ai_prefix(question)
        if prefixed_question is None:
            return
        question = prefixed_question

    if not question:
        await ai_chat.finish(Message("你叫我啦？把问题写在猎宝后面就行。"))

    logger.info(f"AI chat triggered by {event.get_user_id()}: {question[:60]}")

    max_question_length = get_int_env("AI_MAX_QUESTION_LENGTH", DEFAULT_MAX_QUESTION_LENGTH)
    if len(question) > max_question_length:
        await ai_chat.finish(Message(f"问题太长啦，先控制在{max_question_length}字以内。"))

    try:
        answer = await ask_ai(question)
    except asyncio.TimeoutError:
        answer = "AI想太久了，稍后再试一下。"
    except Exception:
        logger.exception("AI chat request failed")
        answer = "AI调用失败了，稍后再试一下。"

    if isinstance(event, GroupMessageEvent):
        try:
            if await is_group_feature_enabled(str(event.group_id), FEATURE_COLLECTOR):
                await save_ai_reply(
                    group_id=event.group_id,
                    bot_user_id=bot.self_id,
                    answer=answer,
                    source_message_id=getattr(event, "message_id", None),
                    source_user_id=event.user_id,
                )
        except Exception:
            logger.exception("Failed to archive AI reply")

    await ai_chat.finish(Message(answer))
