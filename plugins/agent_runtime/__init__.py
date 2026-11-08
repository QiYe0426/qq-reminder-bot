"""Agent runtime 观测层：run trace 的持久化与记录器。

设计约束（与 AGENTS.md 中的 Agent Runtime 原则一致）：

- 只观测，不改变任何业务行为：记录器内部吞掉所有异常，绝不向调用方抛出；
- 独立的 SQLite 文件（默认 ``data/agent_runs.db``），不污染业务库；
- 写库内容做长度截断，避免 prompt / 工具结果把库撑爆；
- 通过 ``AGENT_RUN_TRACE_ENABLED=0`` 可整体关闭。
"""

from .chat import answer_v3, engine_name, should_use_v3
from .router import (
    reactions_routed_for,
    run_deterministic_reactions,
    runtime_active_for,
    runtime_enabled,
    runtime_group_ids,
    runtime_state,
    tier_allows_chat,
)
from .tier import decide_trigger, get_tier_config, init_tier_db, save_tier_config
from .recorder import RunRecorder, chat_key_for, current_recorder, start_run
from .store import (
    DB_PATH,
    RETENTION_DAYS,
    append_step,
    get_run,
    init_runtime_db,
    insert_run,
    list_runs,
    prune_runs,
    runtime_stats,
    today_totals,
    update_run,
)

__all__ = [
    "DB_PATH",
    "RETENTION_DAYS",
    "RunRecorder",
    "append_step",
    "answer_v3",
    "chat_key_for",
    "current_recorder",
    "decide_trigger",
    "engine_name",
    "get_run",
    "get_tier_config",
    "init_runtime_db",
    "init_tier_db",
    "insert_run",
    "list_runs",
    "prune_runs",
    "reactions_routed_for",
    "run_deterministic_reactions",
    "runtime_active_for",
    "runtime_enabled",
    "runtime_group_ids",
    "runtime_state",
    "runtime_stats",
    "save_tier_config",
    "should_use_v3",
    "start_run",
    "tier_allows_chat",
    "today_totals",
    "update_run",
]
