"""Agent runtime 观测层：run trace 的持久化与记录器。

设计约束（与 AGENTS.md 中的 Agent Runtime 原则一致）：

- 只观测，不改变任何业务行为：记录器内部吞掉所有异常，绝不向调用方抛出；
- 独立的 SQLite 文件（默认 ``data/agent_runs.db``），不污染业务库；
- 写库内容做长度截断，避免 prompt / 工具结果把库撑爆；
- 通过 ``AGENT_RUN_TRACE_ENABLED=0`` 可整体关闭。
"""

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
    update_run,
)

__all__ = [
    "DB_PATH",
    "RETENTION_DAYS",
    "RunRecorder",
    "append_step",
    "chat_key_for",
    "current_recorder",
    "get_run",
    "init_runtime_db",
    "insert_run",
    "list_runs",
    "prune_runs",
    "runtime_stats",
    "start_run",
    "update_run",
]
