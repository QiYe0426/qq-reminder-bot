"""统一对话路由（批 1）：确定性规则接管 + 触发档位 R0。

运行条件（全部满足才接管，默认全部关闭）：
- ``AGENT_RUNTIME_ROUTING=1``
- 群号在 ``AGENT_RUNTIME_GROUP_IDS``（缺省回退 ``AI_V3_GROUP_IDS``）

接管后：
1. 先跑确定性回怼（固定回怼 / 关键词回怼 / 调戏 bot），命中即返回，**不进 LLM**；
2. 未命中再按档位决定是否让这条消息进入对话（默认档位关闭）。
"""

from __future__ import annotations

import os
import time
from typing import Any

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent
from nonebot.log import logger

from . import store, tier
from .recorder import chat_key_for, start_run

ENV_RUNTIME = "AGENT_RUNTIME_ROUTING"
ENV_RUNTIME_GROUPS = "AGENT_RUNTIME_GROUP_IDS"
ENV_REACTIONS = "GROUP_REACTIONS_ROUTED_BY_RUNTIME"

_TRUTHY = {"1", "true", "yes", "on"}


def _env_on(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or "").strip().lower() in _TRUTHY


def runtime_enabled() -> bool:
    return _env_on(ENV_RUNTIME)


def runtime_group_ids() -> set[str]:
    raw = os.getenv(ENV_RUNTIME_GROUPS) or os.getenv("AI_V3_GROUP_IDS") or ""
    return {item.strip() for item in raw.replace("，", ",").split(",") if item.strip()}


def runtime_active_for(event: Any) -> bool:
    if not runtime_enabled():
        return False
    group_id = getattr(event, "group_id", None)
    if group_id is None:
        return False
    return str(group_id) in runtime_group_ids()


def reactions_routed_for(event: Any) -> bool:
    """确定性回怼是否交给 router 执行（避免与旧 matcher 双发）。"""
    if not runtime_active_for(event):
        return False
    return _env_on(ENV_REACTIONS, "1")


def runtime_state() -> dict[str, Any]:
    return {
        "enabled": runtime_enabled(),
        "groups": sorted(runtime_group_ids()),
        "reactions_routed": _env_on(ENV_REACTIONS, "1"),
    }


async def _trace_rule(event: MessageEvent, handler: str, text: str) -> None:
    """确定性动作也留一条 trace，控制台能看出“这条为什么被回”。"""
    try:
        group_id = getattr(event, "group_id", None)
        recorder = await start_run(
            chat_key=chat_key_for("group" if group_id is not None else "private", str(group_id)),
            group_id=str(group_id) if group_id is not None else None,
            kind="group" if group_id is not None else "private",
            engine="v3",
            route="R0",
            trigger_summary=text,
            trigger={"raw": text, "handler": handler},
            model="rule",
        )
        await recorder.record_tool(
            tool_name="rule:" + handler,
            args={"text": text},
            result={"sent": True},
            ok=True,
            duration_ms=0,
        )
        await recorder.finish(status="ok", finish_reason=handler)
    except Exception:  # pragma: no cover - 观测层不影响业务
        pass


async def run_deterministic_reactions(bot: Bot, event: MessageEvent) -> bool:
    """执行确定性回怼；返回是否已经回复（回复即不再进 LLM）。"""
    if getattr(event, "group_id", None) is None:
        return False
    if not reactions_routed_for(event):
        return False
    text = event.get_plaintext() or ""
    try:
        from plugins.group_reactions import maybe_bot_tease, maybe_keyword_retort

        if await maybe_keyword_retort(bot, event):
            await _trace_rule(event, "keyword_retort", text)
            return True
        if await maybe_bot_tease(bot, event):
            await _trace_rule(event, "bot_tease", text)
            return True
    except Exception:
        logger.exception("runtime deterministic reactions failed")
    return False


async def tier_allows_chat(
    event: MessageEvent,
    text: str,
    *,
    mentioned: bool = False,
) -> bool:
    """R0 档位判断：是否允许把这条消息送入对话。"""
    group_id = getattr(event, "group_id", None)
    if group_id is None:
        return False
    if mentioned:
        return True
    try:
        stats = await store.chat_run_stats(chat_key_for("group", str(group_id)))
        allowed, reason = await tier.decide_trigger(
            group_id=str(group_id),
            text=text,
            mentioned=mentioned,
            run_count_today=int(stats["runs_today"]),
            last_run_at=int(stats["last_started"]) or None,
        )
    except Exception:
        logger.exception("tier decision failed, defaulting to no reply")
        return False
    if not allowed:
        logger.info("runtime trigger skipped: group={} reason={}", str(group_id), reason)
    return allowed


def describe_trigger_settings(config: dict[str, Any]) -> dict[str, Any]:
    """给控制台用的摘要（估算成本用）。"""
    probability = float(config.get("probability") or 0)
    return {
        **config,
        "estimated_daily_calls": None,
        "note": "概率插话按百分比生效；实际次数还受冷却与每日上限约束。",
        "probability_percent": probability,
    }


def now_seconds() -> int:
    return int(time.time())
