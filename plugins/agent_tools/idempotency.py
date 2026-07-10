from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from .call_identity import arguments_digest, build_idempotency_key, canonical_arguments
from .contracts import ToolResult


DB_PATH = Path("data/agent_tool_executions.db")

STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_UNKNOWN = "unknown"
STATUS_EXPIRED = "expired"

FAILURE_PERMANENT = "permanent"
FAILURE_TEMPORARY = "temporary"
FAILURE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class ExecutionBinding:
    idempotency_key: str
    tool_name: str
    arguments: dict[str, object]
    arguments_hash: str
    user_id: str
    target_type: str
    target_id: str
    effective_group_id: str


@dataclass(frozen=True)
class ExecutionRecord:
    idempotency_key: str
    status: str
    failure_class: str
    result: ToolResult | None
    result_summary: str
    side_effect_state: dict[str, object]
    created_at: str
    updated_at: str
    expires_at: str
    lease_expires_at: str


@dataclass(frozen=True)
class ClaimResult:
    action: str
    record: ExecutionRecord | None = None
    owner_token: str = ""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def owner_token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_execution_binding(
    *,
    tool_name: str,
    arguments: dict[str, object],
    user_id: str,
    target_type: str,
    target_id: str,
    effective_group_id: str,
) -> ExecutionBinding:
    return ExecutionBinding(
        idempotency_key=build_idempotency_key(
            tool_name=tool_name,
            arguments=arguments,
            user_id=user_id,
            target_type=target_type,
            target_id=target_id,
            effective_group_id=effective_group_id,
        ),
        tool_name=str(tool_name),
        arguments=dict(arguments),
        arguments_hash=arguments_digest(arguments),
        user_id=str(user_id),
        target_type=str(target_type),
        target_id=str(target_id),
        effective_group_id=str(effective_group_id),
    )


async def init_idempotency_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_tool_executions (
                idempotency_key TEXT PRIMARY KEY,
                tool_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                arguments_hash TEXT NOT NULL,
                user_id TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                effective_group_id TEXT NOT NULL,
                status TEXT NOT NULL,
                owner_token_hash TEXT NOT NULL,
                failure_class TEXT NOT NULL DEFAULT '',
                result_json TEXT NOT NULL DEFAULT '',
                result_summary TEXT NOT NULL DEFAULT '',
                side_effect_state_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                lease_expires_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_tool_executions_status_expiry
            ON agent_tool_executions (status, expires_at)
            """
        )
        await db.commit()


def decode_json_object(raw: str) -> dict[str, object] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def execution_record_from_row(row: aiosqlite.Row | None) -> ExecutionRecord | None:
    if row is None:
        return None
    return ExecutionRecord(
        idempotency_key=str(row["idempotency_key"]),
        status=str(row["status"]),
        failure_class=str(row["failure_class"] or ""),
        result=decode_json_object(str(row["result_json"] or "")),
        result_summary=str(row["result_summary"] or ""),
        side_effect_state=decode_json_object(str(row["side_effect_state_json"] or "")) or {},
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        expires_at=str(row["expires_at"]),
        lease_expires_at=str(row["lease_expires_at"]),
    )


async def execution_row(db: aiosqlite.Connection, idempotency_key: str) -> aiosqlite.Row | None:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM agent_tool_executions WHERE idempotency_key = ?",
        (idempotency_key,),
    )
    return await cursor.fetchone()


async def claim_execution(
    binding: ExecutionBinding,
    *,
    ttl_seconds: int,
    lease_seconds: int,
) -> ClaimResult:
    await init_idempotency_db()
    current = now_utc()
    current_text = timestamp(current)
    expires_at = timestamp(current + timedelta(seconds=max(1, int(ttl_seconds))))
    lease_expires_at = timestamp(current + timedelta(seconds=max(1, int(lease_seconds))))
    owner_token = secrets.token_urlsafe(32)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        row = await execution_row(db, binding.idempotency_key)
        if row is None:
            try:
                await db.execute(
                    """
                    INSERT INTO agent_tool_executions (
                        idempotency_key, tool_name, arguments_json, arguments_hash,
                        user_id, target_type, target_id, effective_group_id,
                        status, owner_token_hash, created_at, updated_at,
                        expires_at, lease_expires_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        binding.idempotency_key,
                        binding.tool_name,
                        canonical_arguments(binding.arguments),
                        binding.arguments_hash,
                        binding.user_id,
                        binding.target_type,
                        binding.target_id,
                        binding.effective_group_id,
                        STATUS_RUNNING,
                        owner_token_digest(owner_token),
                        current_text,
                        current_text,
                        expires_at,
                        lease_expires_at,
                    ),
                )
                await db.commit()
                return ClaimResult("execute", owner_token=owner_token)
            except sqlite3.IntegrityError:
                await db.rollback()
                return await claim_execution(
                    binding,
                    ttl_seconds=ttl_seconds,
                    lease_seconds=lease_seconds,
                )

        record = execution_record_from_row(row)
        assert record is not None
        if record.status == STATUS_RUNNING:
            if record.lease_expires_at > current_text:
                await db.rollback()
                return ClaimResult("running", record=record)
            await db.execute(
                """
                UPDATE agent_tool_executions
                SET status = ?, failure_class = ?, updated_at = ?
                WHERE idempotency_key = ? AND status = ?
                """,
                (
                    STATUS_UNKNOWN,
                    FAILURE_UNKNOWN,
                    current_text,
                    binding.idempotency_key,
                    STATUS_RUNNING,
                ),
            )
            await db.commit()
            refreshed = execution_record_from_row(await execution_row(db, binding.idempotency_key))
            return ClaimResult("unknown", record=refreshed)

        if record.status == STATUS_UNKNOWN:
            await db.rollback()
            return ClaimResult("unknown", record=record)

        if record.status in {STATUS_SUCCEEDED, STATUS_FAILED} and record.expires_at > current_text:
            if record.result is None:
                await db.execute(
                    """
                    UPDATE agent_tool_executions
                    SET status = ?, failure_class = ?, updated_at = ?
                    WHERE idempotency_key = ?
                    """,
                    (STATUS_UNKNOWN, FAILURE_UNKNOWN, current_text, binding.idempotency_key),
                )
                await db.commit()
                return ClaimResult("unknown", record=record)
            await db.rollback()
            return ClaimResult("cached", record=record)

        if record.status in {STATUS_SUCCEEDED, STATUS_FAILED}:
            await db.execute(
                """
                UPDATE agent_tool_executions
                SET status = ?, updated_at = ?
                WHERE idempotency_key = ?
                """,
                (STATUS_EXPIRED, current_text, binding.idempotency_key),
            )
            await db.commit()
            return await claim_execution(
                binding,
                ttl_seconds=ttl_seconds,
                lease_seconds=lease_seconds,
            )

        cursor = await db.execute(
            """
            UPDATE agent_tool_executions
            SET tool_name = ?, arguments_json = ?, arguments_hash = ?,
                user_id = ?, target_type = ?, target_id = ?, effective_group_id = ?,
                status = ?, owner_token_hash = ?, failure_class = '',
                result_json = '', result_summary = '', side_effect_state_json = '{}',
                created_at = ?, updated_at = ?, expires_at = ?, lease_expires_at = ?
            WHERE idempotency_key = ? AND status = ?
            """,
            (
                binding.tool_name,
                canonical_arguments(binding.arguments),
                binding.arguments_hash,
                binding.user_id,
                binding.target_type,
                binding.target_id,
                binding.effective_group_id,
                STATUS_RUNNING,
                owner_token_digest(owner_token),
                current_text,
                current_text,
                expires_at,
                lease_expires_at,
                binding.idempotency_key,
                STATUS_EXPIRED,
            ),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            return ClaimResult("unknown", record=record)
        await db.commit()
        return ClaimResult("execute", owner_token=owner_token)


def classify_failure(
    result: ToolResult,
    *,
    handler_exception: bool,
    temporary_errors: frozenset[str],
    unknown_errors: frozenset[str],
) -> str:
    if handler_exception:
        return FAILURE_UNKNOWN
    if result.get("ok") is True:
        return ""
    error = str(result.get("error") or "")
    if error in unknown_errors:
        return FAILURE_UNKNOWN
    if result.get("retryable") is True or error in temporary_errors:
        return FAILURE_TEMPORARY
    return FAILURE_PERMANENT


def result_summary(result: ToolResult) -> str:
    summary = {
        "ok": result.get("ok") is True,
        "error": str(result.get("error") or ""),
        "message": str(result.get("message") or "")[:500],
    }
    return json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


SIDE_EFFECT_RESULT_FIELDS: dict[str, tuple[str, ...]] = {
    "create_reminder": ("id", "remind_at", "target_type", "group_id", "target_user_id"),
    "cancel_reminder": ("id",),
    "set_chime": ("target_type", "target_id", "enabled", "mode", "updated_at"),
    "set_group_features": ("group_id", "updated_features", "updated_limits"),
    "generate_daily_report": ("group_id", "date", "filename", "reused_existing"),
    "build_semantic_graph": ("graph_id", "group_id", "node_count", "edge_count"),
    "render_semantic_graph": ("graph_id", "group_id", "image_filename", "image_path", "sent", "send_error"),
}


def side_effect_state(tool_name: str, result: ToolResult) -> dict[str, object]:
    data = result.get("data")
    values = data if isinstance(data, dict) else {}
    state = {
        key: values[key]
        for key in SIDE_EFFECT_RESULT_FIELDS.get(tool_name, ())
        if key in values
    }
    state["result_ok"] = result.get("ok") is True
    if result.get("error"):
        state["error"] = str(result["error"])
    return state


async def complete_execution(
    binding: ExecutionBinding,
    *,
    owner_token: str,
    result: ToolResult,
    failure_class: str,
    ttl_seconds: int,
    temporary_failure_ttl_seconds: int,
) -> bool:
    await init_idempotency_db()
    current = now_utc()
    status = STATUS_SUCCEEDED if result.get("ok") is True else STATUS_FAILED
    if failure_class == FAILURE_UNKNOWN:
        status = STATUS_UNKNOWN
    result_ttl = max(1, int(ttl_seconds))
    if failure_class == FAILURE_TEMPORARY:
        result_ttl = min(result_ttl, max(1, int(temporary_failure_ttl_seconds)))
    expires_at = timestamp(current + timedelta(seconds=result_ttl))

    try:
        result_json = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        side_effect_json = json.dumps(
            side_effect_state(binding.tool_name, result),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        status = STATUS_UNKNOWN
        failure_class = FAILURE_UNKNOWN
        result_json = ""
        side_effect_json = "{}"

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE agent_tool_executions
            SET status = ?, failure_class = ?, result_json = ?,
                result_summary = ?, side_effect_state_json = ?,
                updated_at = ?, expires_at = ?
            WHERE idempotency_key = ? AND status = ? AND owner_token_hash = ?
            """,
            (
                status,
                failure_class,
                result_json,
                result_summary(result),
                side_effect_json,
                timestamp(current),
                expires_at,
                binding.idempotency_key,
                STATUS_RUNNING,
                owner_token_digest(owner_token),
            ),
        )
        await db.commit()
        return cursor.rowcount == 1
