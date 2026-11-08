"""run trace 存储层。

两张表：

``agent_runs``
    一次「运行」= 一条（或一批）消息触发的一次完整处理。

``agent_run_steps``
    运行内部的有序步骤：思考 / 工具调用 / 检索 / 发送 / 错误。

字段按控制台最终形态预留（route / engine / usage / finish_reason 等），
避免 P4 补控制台时再改表回填。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import aiosqlite

DB_PATH = Path(os.getenv("AGENT_RUN_DB_PATH", "data/agent_runs.db"))
RETENTION_DAYS = max(1, int(os.getenv("AGENT_RUN_RETENTION_DAYS", "14")))

# run 表允许更新的字段（白名单，避免拼接任意列名）
_UPDATABLE_RUN_FIELDS = {
    "status",
    "route",
    "engine",
    "trigger_summary",
    "trigger_json",
    "system_prompt",
    "user_prompt",
    "model",
    "usage_prompt_tokens",
    "usage_completion_tokens",
    "usage_cached_tokens",
    "cost",
    "step_count",
    "error",
    "finish_reason",
    "finished_at",
}

_CREATE_RUNS_SQL = """
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id                  TEXT PRIMARY KEY,
    chat_key                TEXT NOT NULL,
    group_id                TEXT,
    kind                    TEXT,
    engine                  TEXT,
    route                   TEXT,
    status                  TEXT NOT NULL,
    trigger_summary         TEXT,
    trigger_json            TEXT,
    system_prompt           TEXT,
    user_prompt             TEXT,
    model                   TEXT,
    usage_prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    usage_completion_tokens INTEGER NOT NULL DEFAULT 0,
    usage_cached_tokens     INTEGER NOT NULL DEFAULT 0,
    cost                    REAL,
    step_count              INTEGER NOT NULL DEFAULT 0,
    error                   TEXT,
    finish_reason           TEXT,
    started_at              INTEGER NOT NULL,
    finished_at             INTEGER
)
"""

_CREATE_STEPS_SQL = """
CREATE TABLE IF NOT EXISTS agent_run_steps (
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    tool_name   TEXT,
    args_json   TEXT,
    result      TEXT,
    ok          INTEGER,
    duration_ms INTEGER,
    meta_json   TEXT,
    at          INTEGER NOT NULL,
    PRIMARY KEY (run_id, seq)
)
"""

_CREATE_INDEXES_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_chat ON agent_runs(chat_key, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_started ON agent_runs(started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_agent_run_steps_run ON agent_run_steps(run_id, seq)",
)


def _dumps(value: Any, limit: int) -> str | None:
    """序列化为 JSON 并截断，None / 空值返回 None。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    if len(text) > limit:
        return text[:limit] + "…<truncated>"
    return text


def _loads(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


_initialized_paths: set[str] = set()


async def init_runtime_db(path: Path | None = None) -> None:
    db_path = path or DB_PATH
    key = str(db_path)
    if key in _initialized_paths:
        return
    # trace 里含 prompt 与用户原文，按项目约定在 POSIX 上收紧权限（0700/0600）。
    from plugins.agent_tools.sensitive_storage import prepare_sensitive_sqlite_path

    prepare_sensitive_sqlite_path(db_path)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(_CREATE_RUNS_SQL)
        await db.execute(_CREATE_STEPS_SQL)
        for statement in _CREATE_INDEXES_SQL:
            await db.execute(statement)
        await db.commit()
    _initialized_paths.add(key)


async def insert_run(
    run_id: str,
    *,
    chat_key: str,
    group_id: str | None = None,
    kind: str | None = None,
    engine: str = "legacy",
    route: str | None = None,
    trigger_summary: str | None = None,
    trigger: Any = None,
    model: str | None = None,
    started_at: int | None = None,
    path: Path | None = None,
) -> None:
    db_path = path or DB_PATH
    await init_runtime_db(db_path)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO agent_runs (
                run_id, chat_key, group_id, kind, engine, route, status,
                trigger_summary, trigger_json, model, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)
            """,
            (
                run_id,
                chat_key,
                group_id,
                kind,
                engine,
                route,
                (trigger_summary or "")[:500] or None,
                _dumps(trigger, 4000),
                model,
                started_at or int(time.time()),
            ),
        )
        await db.commit()


async def update_run(run_id: str, *, path: Path | None = None, **fields: Any) -> None:
    updates: list[str] = []
    values: list[Any] = []
    for key, value in fields.items():
        if key not in _UPDATABLE_RUN_FIELDS:
            raise ValueError(f"字段不允许更新：{key}")
        updates.append(f"{key} = ?")
        values.append(value)
    if not updates:
        return
    values.append(run_id)
    db_path = path or DB_PATH
    async with aiosqlite.connect(db_path) as db:
        await db.execute(f"UPDATE agent_runs SET {', '.join(updates)} WHERE run_id = ?", values)
        await db.commit()


async def append_step(
    run_id: str,
    *,
    kind: str,
    tool_name: str | None = None,
    args: Any = None,
    result: Any = None,
    ok: bool | None = None,
    duration_ms: int | None = None,
    meta: Any = None,
    at: int | None = None,
    path: Path | None = None,
) -> int:
    db_path = path or DB_PATH
    await init_runtime_db(db_path)
    timestamp = at or int(time.time())
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM agent_run_steps WHERE run_id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        seq = int(row[0]) if row else 1
        await db.execute(
            """
            INSERT INTO agent_run_steps (
                run_id, seq, kind, tool_name, args_json, result, ok, duration_ms, meta_json, at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                seq,
                kind,
                tool_name,
                _dumps(args, 4000),
                _dumps(result, 8000),
                None if ok is None else (1 if ok else 0),
                duration_ms,
                _dumps(meta, 2000),
                timestamp,
            ),
        )
        await db.execute(
            "UPDATE agent_runs SET step_count = ? WHERE run_id = ?",
            (seq, run_id),
        )
        await db.commit()
        return seq


def _run_row_to_dict(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = {key: row[key] for key in row.keys()}
    data["trigger"] = _loads(data.get("trigger_json"), None)
    return data


def _step_row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    data = {key: row[key] for key in row.keys()}
    data["args"] = _loads(data.get("args_json"), None)
    data["meta"] = _loads(data.get("meta_json"), None)
    data["ok"] = bool(data["ok"]) if data.get("ok") is not None else None
    return data


async def list_runs(
    *,
    chat_key: str | None = None,
    status: str | None = None,
    limit: int = 50,
    before: int | None = None,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    db_path = path or DB_PATH
    await init_runtime_db(db_path)
    clauses: list[str] = []
    values: list[Any] = []
    if chat_key:
        clauses.append("chat_key = ?")
        values.append(chat_key)
    if status:
        clauses.append("status = ?")
        values.append(status)
    if before:
        clauses.append("started_at < ?")
        values.append(int(before))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    safe_limit = max(1, min(int(limit), 200))
    values.append(safe_limit)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT run_id, chat_key, group_id, kind, engine, route, status,
                   trigger_summary, model, usage_prompt_tokens, usage_completion_tokens,
                   cost, step_count, error, finish_reason, started_at, finished_at
            FROM agent_runs
            {where}
            ORDER BY started_at DESC
            LIMIT ?
            """,
            values,
        )
        rows = await cursor.fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


async def get_run(run_id: str, *, path: Path | None = None) -> dict[str, Any] | None:
    db_path = path or DB_PATH
    await init_runtime_db(db_path)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,))
        run_row = await cursor.fetchone()
        if run_row is None:
            return None
        cursor = await db.execute(
            "SELECT * FROM agent_run_steps WHERE run_id = ? ORDER BY seq ASC",
            (run_id,),
        )
        step_rows = await cursor.fetchall()
    run = _run_row_to_dict(run_row)
    assert run is not None
    run["steps"] = [_step_row_to_dict(row) for row in step_rows]
    return run


async def prune_runs(
    *,
    retention_days: int | None = None,
    path: Path | None = None,
) -> int:
    db_path = path or DB_PATH
    await init_runtime_db(db_path)
    cutoff = int(time.time()) - max(1, retention_days or RETENTION_DAYS) * 86400
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "DELETE FROM agent_run_steps WHERE run_id IN (SELECT run_id FROM agent_runs WHERE started_at < ?)",
            (cutoff,),
        )
        await cursor.fetchall()
        cursor = await db.execute("DELETE FROM agent_runs WHERE started_at < ?", (cutoff,))
        deleted = cursor.rowcount or 0
        await db.commit()
    return deleted


async def runtime_stats(*, path: Path | None = None) -> dict[str, Any]:
    db_path = path or DB_PATH
    await init_runtime_db(db_path)
    today_start = int(time.time()) - int(time.time()) % 86400
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors,
                SUM(CASE WHEN started_at >= ? THEN 1 ELSE 0 END) AS today
            FROM agent_runs
            """,
            (today_start,),
        )
        row = await cursor.fetchone()
    return {
        "total": int(row["total"] or 0) if row else 0,
        "errors": int(row["errors"] or 0) if row else 0,
        "today": int(row["today"] or 0) if row else 0,
    }


async def today_totals(*, path: Path | None = None) -> dict[str, Any]:
    """今日概览：运行数、tokens、错误数。控制台状态栏与用量页用。"""
    db_path = path or DB_PATH
    await init_runtime_db(db_path)
    today_start = int(time.time()) - int(time.time()) % 86400
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                COUNT(*) AS runs,
                COALESCE(SUM(usage_prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(usage_completion_tokens), 0) AS completion_tokens,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors
            FROM agent_runs
            WHERE started_at >= ?
            """,
            (today_start,),
        )
        row = await cursor.fetchone()
    return {
        "runs": int(row["runs"] or 0) if row else 0,
        "prompt_tokens": int(row["prompt_tokens"] or 0) if row else 0,
        "completion_tokens": int(row["completion_tokens"] or 0) if row else 0,
        "errors": int(row["errors"] or 0) if row else 0,
    }


async def chat_run_stats(chat_key: str, *, path: Path | None = None) -> dict[str, Any]:
    """某个会话的运行统计：今日次数与最近一次开始时间（R0 档位用）。"""
    db_path = path or DB_PATH
    await init_runtime_db(db_path)
    today_start = int(time.time()) - int(time.time()) % 86400
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT COUNT(*) AS runs, MAX(started_at) AS last_started
            FROM agent_runs
            WHERE chat_key = ? AND started_at >= ?
            """,
            (chat_key, today_start),
        )
        row = await cursor.fetchone()
        cursor = await db.execute(
            "SELECT MAX(started_at) AS last_started FROM agent_runs WHERE chat_key = ?",
            (chat_key,),
        )
        last_row = await cursor.fetchone()
    return {
        "runs_today": int(row["runs"] or 0) if row else 0,
        "last_started": int(last_row["last_started"] or 0) if last_row else 0,
    }
