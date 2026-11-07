"""Run 记录器。

调用方只需要在 run 开始处 ``await start_run(...)``，随后在各关键点调用
``record_*`` 方法即可。所有方法：

- **绝不抛异常**（内部 try/except，最坏情况是丢掉一条 trace）；
- 在网络请求 / 数据库不可用时静默降级；
- 关闭追踪（``AGENT_RUN_TRACE_ENABLED=0``）时整体退化为 O(1) 空操作。
"""

from __future__ import annotations

import os
import time
import uuid
from contextvars import ContextVar
from typing import Any

from . import store

_current: ContextVar["RunRecorder | None"] = ContextVar("agent_run_recorder", default=None)

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def tracing_enabled() -> bool:
    raw = os.getenv("AGENT_RUN_TRACE_ENABLED", "1").strip().lower()
    return raw not in _FALSY


def chat_key_for(kind: str, target_id: str) -> str:
    """统一会话键：``group_123`` / ``private_456``。"""
    kind = (kind or "").strip().lower() or "unknown"
    target_id = str(target_id or "").strip() or "unknown"
    return f"{kind}_{target_id}"


def current_recorder() -> "RunRecorder | None":
    return _current.get()


def new_run_id() -> str:
    return f"run_{int(time.time() * 1000):013d}_{uuid.uuid4().hex[:8]}"


class RunRecorder:
    """一次运行的写入句柄。"""

    def __init__(self, run_id: str, *, enabled: bool = True) -> None:
        self.run_id = run_id
        self.enabled = enabled
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._cached_tokens = 0
        self._llm_turns = 0
        self._sent_count = 0
        self._finished = False

    @property
    def sent_count(self) -> int:
        return self._sent_count

    # ── 生命周期 ────────────────────────────────────────────────

    @classmethod
    async def start(
        cls,
        *,
        chat_key: str,
        group_id: str | None = None,
        kind: str | None = None,
        engine: str = "legacy",
        route: str | None = None,
        trigger_summary: str | None = None,
        trigger: Any = None,
        model: str | None = None,
    ) -> "RunRecorder":
        recorder = cls(new_run_id(), enabled=tracing_enabled())
        if not recorder.enabled:
            return recorder
        try:
            await store.insert_run(
                recorder.run_id,
                chat_key=chat_key,
                group_id=group_id,
                kind=kind,
                engine=engine,
                route=route,
                trigger_summary=trigger_summary,
                trigger=trigger,
                model=model,
            )
        except Exception:  # pragma: no cover - 观测层不允许影响业务
            recorder.enabled = False
            return recorder
        _current.set(recorder)
        try:
            await store.prune_runs()
        except Exception:  # pragma: no cover
            pass
        return recorder

    async def set_prompts(
        self,
        *,
        system_prompt: str | None = None,
        user_prompt: str | None = None,
        model: str | None = None,
    ) -> None:
        if not self._usable():
            return
        try:
            fields: dict[str, Any] = {}
            if system_prompt is not None:
                fields["system_prompt"] = system_prompt[:20000]
            if user_prompt is not None:
                fields["user_prompt"] = user_prompt[:8000]
            if model is not None:
                fields["model"] = model
            if fields:
                await store.update_run(self.run_id, **fields)
        except Exception:  # pragma: no cover
            self.enabled = False

    async def set_route(self, route: str, **extra: Any) -> None:
        if not self._usable():
            return
        try:
            await store.update_run(self.run_id, route=route, **{k: v for k, v in extra.items() if k in {"engine"}})
        except Exception:  # pragma: no cover
            self.enabled = False

    # ── 步骤 ────────────────────────────────────────────────────

    async def record_llm_turn(self, *, usage: Any = None, content: str | None = None) -> None:
        """记录一次模型调用：累计 token，内容非空时写一条「思考」步骤。"""
        self._llm_turns += 1
        if usage is not None:
            self._prompt_tokens += _usage_value(usage, "prompt_tokens")
            self._completion_tokens += _usage_value(usage, "completion_tokens")
            self._cached_tokens += _usage_cached(usage)
        if not self._usable():
            return
        try:
            if content and content.strip():
                await store.append_step(
                    self.run_id,
                    kind="think",
                    result=content,
                    meta={"llm_turn": self._llm_turns},
                )
            await store.update_run(
                self.run_id,
                usage_prompt_tokens=self._prompt_tokens,
                usage_completion_tokens=self._completion_tokens,
                usage_cached_tokens=self._cached_tokens,
            )
        except Exception:  # pragma: no cover
            self.enabled = False

    async def record_tool(
        self,
        *,
        tool_name: str,
        args: Any = None,
        result: Any = None,
        ok: bool = True,
        duration_ms: int | None = None,
    ) -> None:
        if not self._usable():
            return
        try:
            await store.append_step(
                self.run_id,
                kind="tool_call",
                tool_name=tool_name,
                args=args,
                result=result,
                ok=ok,
                duration_ms=duration_ms,
            )
        except Exception:  # pragma: no cover
            self.enabled = False

    async def record_retrieval(
        self,
        *,
        need: str,
        queries: Any = None,
        hits: int | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """R2 检索档预留：记录判定结论与检索结果规模。"""
        if not self._usable():
            return
        try:
            await store.append_step(
                self.run_id,
                kind="retrieval",
                args={"need": need, "queries": queries},
                result={"hits": hits},
                duration_ms=duration_ms,
            )
        except Exception:  # pragma: no cover
            self.enabled = False

    async def record_sent(self, text: str, *, meta: Any = None) -> None:
        self._sent_count += 1
        if not self._usable():
            return
        try:
            await store.append_step(
                self.run_id,
                kind="sent",
                result=text,
                meta=meta or {"source": "agent_return"},
            )
        except Exception:  # pragma: no cover
            self.enabled = False

    # ── 收尾 ────────────────────────────────────────────────────

    async def finish(
        self,
        *,
        status: str = "ok",
        error: str | None = None,
        finish_reason: str | None = None,
    ) -> None:
        if not self._usable() or self._finished:
            return
        self._finished = True
        try:
            await store.update_run(
                self.run_id,
                status=status,
                error=(error[:2000] if error else None),
                finish_reason=finish_reason,
                finished_at=int(time.time()),
            )
        except Exception:  # pragma: no cover
            self.enabled = False
        finally:
            _current.set(None)

    def _usable(self) -> bool:
        return self.enabled and not self._finished and tracing_enabled()


async def start_run(**kwargs: Any) -> RunRecorder:
    """工厂函数：记录器不可用时返回一个禁用的空实现，调用方无需判空。"""
    try:
        return await RunRecorder.start(**kwargs)
    except Exception:  # pragma: no cover - 兜底
        return RunRecorder(new_run_id(), enabled=False)


def _usage_value(usage: Any, name: str) -> int:
    try:
        value = getattr(usage, name, None)
        if value is None and isinstance(usage, dict):
            value = usage.get(name)
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _usage_cached(usage: Any) -> int:
    try:
        details = getattr(usage, "prompt_tokens_details", None)
        if details is None and isinstance(usage, dict):
            details = usage.get("prompt_tokens_details")
        if details is None:
            return 0
        value = getattr(details, "cached_tokens", None)
        if value is None and isinstance(details, dict):
            value = details.get("cached_tokens")
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
