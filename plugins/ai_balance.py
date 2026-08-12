from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

import aiohttp
from nonebot import on_fullmatch
from nonebot.adapters.onebot.v11 import Event, Message, PrivateMessageEvent

from plugins.access_control import admin_denial


DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
REQUEST_TIMEOUT_SECONDS = 10
BALANCE_COMMANDS = ("查询 AI 余额", "查询余额", "AI余额")

logger = logging.getLogger(__name__)
ai_balance = on_fullmatch(BALANCE_COMMANDS, priority=5, block=True)


class DeepSeekBalanceError(RuntimeError):
    """A safe, user-facing DeepSeek balance query error."""


def build_balance_url(base_url: str) -> str:
    normalized = (base_url or DEFAULT_DEEPSEEK_BASE_URL).strip().rstrip("/")
    if normalized.endswith("/v1"):
        normalized = normalized[:-3]
    return f"{normalized}/user/balance"


def _balance_amount(info: Mapping[str, Any], field: str) -> str:
    value = info.get(field)
    if not isinstance(value, str):
        raise DeepSeekBalanceError("DeepSeek 余额响应格式异常，请稍后重试。")
    try:
        Decimal(value)
    except InvalidOperation as exc:
        raise DeepSeekBalanceError("DeepSeek 余额响应格式异常，请稍后重试。") from exc
    return value


def format_deepseek_balance(payload: object) -> str:
    if not isinstance(payload, Mapping):
        raise DeepSeekBalanceError("DeepSeek 余额响应格式异常，请稍后重试。")

    is_available = payload.get("is_available")
    balance_infos = payload.get("balance_infos")
    if not isinstance(is_available, bool) or not isinstance(balance_infos, list):
        raise DeepSeekBalanceError("DeepSeek 余额响应格式异常，请稍后重试。")

    status = "可用" if is_available else "不可用（请检查余额或账户状态）"
    lines = ["DeepSeek API 余额", f"状态：{status}"]
    for info in balance_infos:
        if not isinstance(info, Mapping):
            raise DeepSeekBalanceError("DeepSeek 余额响应格式异常，请稍后重试。")
        currency = info.get("currency")
        if currency not in {"CNY", "USD"}:
            raise DeepSeekBalanceError("DeepSeek 余额响应格式异常，请稍后重试。")
        symbol = "¥" if currency == "CNY" else "$"
        total = _balance_amount(info, "total_balance")
        granted = _balance_amount(info, "granted_balance")
        topped_up = _balance_amount(info, "topped_up_balance")
        lines.append(
            f"{currency}：总余额 {symbol}{total}"
            f"（赠金 {symbol}{granted}，充值 {symbol}{topped_up}）"
        )
    return "\n".join(lines)


async def fetch_deepseek_balance(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    session_factory: Callable[..., Any] = aiohttp.ClientSession,
) -> dict[str, Any]:
    effective_api_key = api_key if api_key is not None else os.getenv("DEEPSEEK_API_KEY", "")
    if not effective_api_key.strip():
        raise DeepSeekBalanceError("DeepSeek API Key 未配置。")

    effective_base_url = (
        base_url
        if base_url is not None
        else os.getenv("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL)
    )
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {effective_api_key}",
    }
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)

    try:
        async with session_factory(timeout=timeout) as session:
            async with session.get(build_balance_url(effective_base_url), headers=headers) as response:
                if response.status != 200:
                    logger.warning(f"DeepSeek balance request failed with HTTP {response.status}")
                    if response.status == 401:
                        raise DeepSeekBalanceError("DeepSeek API Key 无效或已失效。")
                    if response.status == 402:
                        raise DeepSeekBalanceError("DeepSeek API 余额不足，请及时充值。")
                    if response.status == 429:
                        raise DeepSeekBalanceError("DeepSeek 查询请求过于频繁，请稍后重试。")
                    if response.status >= 500:
                        raise DeepSeekBalanceError("DeepSeek 服务暂时不可用，请稍后重试。")
                    raise DeepSeekBalanceError(
                        f"DeepSeek 余额查询失败（HTTP {response.status}），请稍后重试。"
                    )
                try:
                    payload = await response.json()
                except Exception as exc:
                    logger.warning(
                        "DeepSeek balance response JSON decoding failed: "
                        f"{type(exc).__name__}"
                    )
                    raise DeepSeekBalanceError(
                        "DeepSeek 余额响应格式异常，请稍后重试。"
                    ) from exc
    except DeepSeekBalanceError:
        raise
    except asyncio.TimeoutError as exc:
        logger.warning("DeepSeek balance request timed out")
        raise DeepSeekBalanceError("DeepSeek 余额查询请求超时，请稍后重试。") from exc
    except (aiohttp.ClientError, OSError) as exc:
        logger.warning(f"DeepSeek balance network failure: {type(exc).__name__}")
        raise DeepSeekBalanceError("无法连接 DeepSeek 服务，请稍后重试。") from exc

    format_deepseek_balance(payload)
    return dict(payload)


async def balance_command_text(
    event: Event,
    *,
    fetcher: Callable[[], Awaitable[dict[str, Any]]] = fetch_deepseek_balance,
) -> str | None:
    if not isinstance(event, PrivateMessageEvent):
        return None
    if denial := admin_denial(event):
        return denial
    try:
        payload = await fetcher()
        return format_deepseek_balance(payload)
    except DeepSeekBalanceError as exc:
        return str(exc)


@ai_balance.handle()
async def handle_ai_balance(event: Event) -> None:
    text = await balance_command_text(event)
    if text is not None:
        await ai_balance.finish(Message(text))
