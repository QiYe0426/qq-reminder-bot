from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

import aiohttp
from nonebot import on_fullmatch
from nonebot.adapters.onebot.v11 import Event, Message, PrivateMessageEvent

from plugins.access_control import admin_denial


DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
REQUEST_TIMEOUT_SECONDS = 10
BALANCE_COMMANDS = ("查询 AI 余额", "查询余额", "AI余额")
DEFAULT_QWEN_MODEL = "qwen3-vl-flash"
QWEN_MODULE_IMAGE_VISION = "媒体图片识别"
QWEN_MODULE_KEYWORD_SCAN = "关键词回怼图片文字检测"
DEEPSEEK_MODULES = (
    "AI 对话、Agent 工具调度、联网决策、陪伴画像、"
    "日报总结（回退配置）"
)

logger = logging.getLogger(__name__)
ai_balance = on_fullmatch(BALANCE_COMMANDS, priority=5, block=True)


class DeepSeekBalanceError(RuntimeError):
    """A safe, user-facing DeepSeek balance query error."""


@dataclass(frozen=True)
class QwenProbeConfig:
    modules: tuple[str, ...]
    api_key: str
    base_url: str
    model: str


@dataclass(frozen=True)
class QwenProbeResult:
    status: str
    detail: str


def build_balance_url(base_url: str) -> str:
    normalized = (base_url or DEFAULT_DEEPSEEK_BASE_URL).strip().rstrip("/")
    if normalized.endswith("/v1"):
        normalized = normalized[:-3]
    return f"{normalized}/user/balance"


def build_qwen_probe_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


def discover_qwen_probe_configs(
    environ: Mapping[str, str] = os.environ,
) -> list[QwenProbeConfig]:
    image_key = environ.get("IMAGE_VISION_API_KEY") or environ.get("OPENAI_API_KEY") or ""
    image_url = environ.get("IMAGE_VISION_BASE_URL") or environ.get("OPENAI_BASE_URL") or ""
    image_model = environ.get("IMAGE_VISION_MODEL") or DEFAULT_QWEN_MODEL

    keyword_key = (
        environ.get("KEYWORD_RETORT_IMAGE_SCAN_API_KEY") or image_key
    )
    keyword_url = (
        environ.get("KEYWORD_RETORT_IMAGE_SCAN_BASE_URL") or image_url
    )
    keyword_model = (
        environ.get("KEYWORD_RETORT_IMAGE_SCAN_MODEL") or image_model
    )

    discovered: list[tuple[str, str, str, str]] = []
    if image_key.strip() and image_url.strip() and image_model.strip():
        discovered.append(
            (QWEN_MODULE_IMAGE_VISION, image_key.strip(), image_url.strip(), image_model.strip())
        )
    if keyword_key.strip() and keyword_url.strip() and keyword_model.strip():
        discovered.append(
            (
                QWEN_MODULE_KEYWORD_SCAN,
                keyword_key.strip(),
                keyword_url.strip(),
                keyword_model.strip(),
            )
        )

    configs: list[QwenProbeConfig] = []
    positions: dict[tuple[str, str, str], int] = {}
    for module, api_key, base_url, model in discovered:
        identity = (api_key, base_url.rstrip("/"), model)
        existing_position = positions.get(identity)
        if existing_position is None:
            positions[identity] = len(configs)
            configs.append(
                QwenProbeConfig(
                    modules=(module,),
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                )
            )
            continue
        existing = configs[existing_position]
        configs[existing_position] = QwenProbeConfig(
            modules=(*existing.modules, module),
            api_key=existing.api_key,
            base_url=existing.base_url,
            model=existing.model,
        )
    return configs


def _qwen_error_code(payload: object) -> str:
    if not isinstance(payload, Mapping):
        return ""
    direct_code = payload.get("code")
    if isinstance(direct_code, str):
        return direct_code
    error = payload.get("error")
    if isinstance(error, Mapping):
        nested_code = error.get("code")
        if isinstance(nested_code, str):
            return nested_code
    return ""


def _classify_qwen_response(status: int, payload: object) -> QwenProbeResult:
    code = _qwen_error_code(payload).lower()
    if "arrearage" in code or "insufficientbalance" in code:
        return QwenProbeResult(status="欠费", detail="账户欠费，请前往阿里云充值")
    if status == 401 or "invalidapikey" in code or "invalid_api_key" in code:
        return QwenProbeResult(status="Key 无效", detail="API Key 无效或已失效")
    if status == 403 or "accessdenied" in code or "forbidden" in code:
        return QwenProbeResult(status="Key 无效", detail="API Key 无权限或已失效")
    if status == 429 or "throttl" in code or "ratelimit" in code:
        return QwenProbeResult(status="限流", detail="请求过于频繁，请稍后重试")
    if status == 200:
        return QwenProbeResult(status="可用", detail="模型调用正常")
    if status >= 500:
        return QwenProbeResult(status="服务异常", detail="阿里云百炼服务暂时不可用")
    return QwenProbeResult(
        status="服务异常",
        detail=f"Qwen 状态查询失败（HTTP {status}）",
    )


async def probe_qwen_status(
    config: QwenProbeConfig,
    *,
    session_factory: Callable[..., Any] = aiohttp.ClientSession,
) -> QwenProbeResult:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.api_key}",
    }
    request_payload = {
        "model": config.model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    module_label = ",".join(config.modules)
    try:
        async with session_factory(timeout=timeout) as session:
            async with session.post(
                build_qwen_probe_url(config.base_url),
                headers=headers,
                json=request_payload,
            ) as response:
                try:
                    payload = await response.json()
                except Exception as exc:
                    logger.warning(
                        "Qwen status response JSON decoding failed for "
                        f"modules={module_label}: {type(exc).__name__}"
                    )
                    return QwenProbeResult(
                        status="服务异常",
                        detail="Qwen 返回了异常响应",
                    )
                result = _classify_qwen_response(response.status, payload)
                if result.status != "可用":
                    logger.warning(
                        "Qwen status probe failed for "
                        f"modules={module_label}: HTTP {response.status}, "
                        f"category={result.status}"
                    )
                return result
    except asyncio.TimeoutError:
        logger.warning(f"Qwen status probe timed out for modules={module_label}")
        return QwenProbeResult(status="服务异常", detail="Qwen 状态查询超时")
    except (aiohttp.ClientError, OSError) as exc:
        logger.warning(
            "Qwen status network failure for "
            f"modules={module_label}: {type(exc).__name__}"
        )
        return QwenProbeResult(status="服务异常", detail="无法连接阿里云百炼服务")


def format_qwen_status(config: QwenProbeConfig, result: QwenProbeResult) -> str:
    return "\n".join(
        (
            "Qwen / 阿里云百炼",
            f"负责模块：{'、'.join(config.modules)}",
            f"模型：{config.model}",
            f"状态：{result.status}",
            f"说明：{result.detail}",
            "探测说明：最多生成 1 token，不提供人民币余额",
        )
    )


def format_deepseek_status(payload: object) -> str:
    balance_lines = format_deepseek_balance(payload).splitlines()
    return "\n".join(("DeepSeek", f"负责模块：{DEEPSEEK_MODULES}", *balance_lines[1:]))


def _format_deepseek_failure(detail: str) -> str:
    return "\n".join(
        (
            "DeepSeek",
            f"负责模块：{DEEPSEEK_MODULES}",
            "状态：查询失败",
            f"说明：{detail}",
        )
    )


def _format_qwen_unconfigured() -> str:
    return "\n".join(
        (
            "Qwen / 阿里云百炼",
            f"负责模块：{QWEN_MODULE_IMAGE_VISION}、{QWEN_MODULE_KEYWORD_SCAN}",
            "状态：未配置",
            "说明：未找到图片识别 API Key、服务地址或模型配置",
        )
    )


async def build_ai_api_status_report(
    *,
    deepseek_fetcher: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    qwen_configs: Sequence[QwenProbeConfig] | None = None,
    qwen_probe: Callable[[QwenProbeConfig], Awaitable[QwenProbeResult]] = probe_qwen_status,
) -> str:
    effective_deepseek_fetcher = deepseek_fetcher or fetch_deepseek_balance
    effective_configs = (
        list(qwen_configs)
        if qwen_configs is not None
        else discover_qwen_probe_configs()
    )
    awaitables: list[Awaitable[object]] = [effective_deepseek_fetcher()]
    awaitables.extend(qwen_probe(config) for config in effective_configs)
    results = await asyncio.gather(*awaitables, return_exceptions=True)

    deepseek_result = results[0]
    if isinstance(deepseek_result, DeepSeekBalanceError):
        sections = [_format_deepseek_failure(str(deepseek_result))]
    elif isinstance(deepseek_result, BaseException):
        logger.warning(
            "Unexpected DeepSeek balance failure: "
            f"{type(deepseek_result).__name__}"
        )
        sections = [_format_deepseek_failure("DeepSeek 查询异常，请稍后重试")]
    else:
        try:
            sections = [format_deepseek_status(deepseek_result)]
        except DeepSeekBalanceError as exc:
            sections = [_format_deepseek_failure(str(exc))]

    if not effective_configs:
        sections.append(_format_qwen_unconfigured())
    for config, result in zip(effective_configs, results[1:]):
        if isinstance(result, BaseException):
            logger.warning(
                "Unexpected Qwen probe failure for "
                f"modules={','.join(config.modules)}: {type(result).__name__}"
            )
            safe_result = QwenProbeResult(
                status="服务异常",
                detail="Qwen 状态查询异常，请稍后重试",
            )
        else:
            safe_result = result
        sections.append(format_qwen_status(config, safe_result))
    return "AI API 状态\n\n" + "\n\n".join(sections)


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
    qwen_configs: Sequence[QwenProbeConfig] | None = None,
    qwen_probe: Callable[[QwenProbeConfig], Awaitable[QwenProbeResult]] = probe_qwen_status,
) -> str | None:
    if not isinstance(event, PrivateMessageEvent):
        return None
    if denial := admin_denial(event):
        return denial
    return await build_ai_api_status_report(
        deepseek_fetcher=fetcher,
        qwen_configs=qwen_configs,
        qwen_probe=qwen_probe,
    )


@ai_balance.handle()
async def handle_ai_balance(event: Event) -> None:
    text = await balance_command_text(event)
    if text is not None:
        await ai_balance.finish(Message(text))
