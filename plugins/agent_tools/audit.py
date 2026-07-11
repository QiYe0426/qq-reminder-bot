from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

import aiosqlite
from nonebot.log import logger


DB_PATH = Path("data/agent_tool_audit.db")
AUDIT_HMAC_KEY_ENV = "AGENT_TOOL_AUDIT_HMAC_KEY"

_fingerprint_key: bytes | None = None
_fingerprint_key_source = ""
_fingerprint_key_lock = threading.Lock()

SENSITIVE_KEY_PARTS = (
    "prompt",
    "argument",
    "parameter",
    "token",
    "secret",
    "password",
    "authorization",
    "cookie",
    "confirmation_code",
    "owner_token",
    "raw_text",
    "user_text",
    "question",
    "content",
    "message",
    "reminder_text",
    "nickname",
    "display_name",
    "keyword",
    "query",
)

SAFE_STRING_KEYS = {
    "action",
    "date",
    "error",
    "error_code",
    "event_type",
    "failure_class",
    "feature",
    "graph_id",
    "id",
    "mode",
    "outcome",
    "scope",
    "source",
    "stage",
    "status",
    "target_date",
    "target_id",
    "target_type",
    "tool_name",
    "type",
    "user_id",
}


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    invocation_id: str
    sequence: int
    occurred_at: str
    event_type: str
    tool_call_id: str
    tool_name: str
    invocation_source: str
    actor_user_id: str
    session_target_type: str
    session_target_id: str
    effective_group_id: str
    arguments_fingerprint: str
    confirmation_id: int | None
    confirmation_status: str
    idempotency_key: str
    idempotency_status: str
    risk_level: str
    side_effect: str
    execution_stage: str
    outcome: str
    error_code: str
    retryable: bool
    failure_class: str
    duration_ms: int
    safe_details: dict[str, object]


def now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def create_invocation_id() -> str:
    return str(uuid.uuid4())


def _get_fingerprint_key() -> bytes:
    global _fingerprint_key, _fingerprint_key_source
    if _fingerprint_key is not None:
        return _fingerprint_key
    with _fingerprint_key_lock:
        if _fingerprint_key is not None:
            return _fingerprint_key
        configured = os.getenv(AUDIT_HMAC_KEY_ENV, "").strip()
        if configured:
            _fingerprint_key = configured.encode("utf-8")
            _fingerprint_key_source = "configured"
        else:
            _fingerprint_key = secrets.token_bytes(32)
            _fingerprint_key_source = "runtime"
            logger.warning(
                f"{AUDIT_HMAC_KEY_ENV} is not configured; Agent tool audit fingerprints "
                "use a runtime-only random key and cannot be correlated across restarts."
            )
        return _fingerprint_key


def fingerprint_arguments(arguments: Mapping[str, object]) -> str:
    payload = json.dumps(
        dict(arguments),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hmac.new(_get_fingerprint_key(), payload, hashlib.sha256).hexdigest()


def sanitize_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "[REDACTED]"
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return "[REDACTED]"
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        return "[REDACTED]"
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", "", ""))


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower()
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def _sanitize_value(key: str, value: object) -> object:
    normalized_key = key.strip().lower()
    if _is_sensitive_key(normalized_key):
        if isinstance(value, str):
            return {"redacted": True, "length": len(value)}
        return {"redacted": True}
    if isinstance(value, dict):
        return sanitize_details(value)
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(normalized_key, item) for item in value[:50]]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        if normalized_key in {"url", "uri"}:
            return sanitize_url(value)
        if normalized_key in SAFE_STRING_KEYS or normalized_key.endswith("_id") or normalized_key.endswith("_status"):
            return value[:200]
        return {"redacted": True, "length": len(value)}
    return {"redacted": True}


def sanitize_details(details: Mapping[str, object] | None) -> dict[str, object]:
    if not details:
        return {}
    sanitized: dict[str, object] = {}
    for raw_key, value in details.items():
        key = str(raw_key).strip()[:80]
        if not key:
            continue
        sanitized[key] = _sanitize_value(key, value)
    return sanitized


async def init_audit_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_tool_audit_events (
                event_id TEXT PRIMARY KEY,
                invocation_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                occurred_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                tool_call_id TEXT NOT NULL DEFAULT '',
                tool_name TEXT NOT NULL,
                invocation_source TEXT NOT NULL DEFAULT '',
                actor_user_id TEXT NOT NULL DEFAULT '',
                session_target_type TEXT NOT NULL DEFAULT '',
                session_target_id TEXT NOT NULL DEFAULT '',
                effective_group_id TEXT NOT NULL DEFAULT '',
                arguments_fingerprint TEXT NOT NULL DEFAULT '',
                confirmation_id INTEGER,
                confirmation_status TEXT NOT NULL DEFAULT '',
                idempotency_key TEXT NOT NULL DEFAULT '',
                idempotency_status TEXT NOT NULL DEFAULT '',
                risk_level TEXT NOT NULL DEFAULT '',
                side_effect TEXT NOT NULL DEFAULT '',
                execution_stage TEXT NOT NULL DEFAULT '',
                outcome TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                retryable INTEGER NOT NULL DEFAULT 0,
                failure_class TEXT NOT NULL DEFAULT '',
                duration_ms INTEGER NOT NULL DEFAULT 0,
                safe_details_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE (invocation_id, sequence)
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_tool_audit_occurred
            ON agent_tool_audit_events (occurred_at)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_tool_audit_tool_time
            ON agent_tool_audit_events (tool_name, occurred_at)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_tool_audit_invocation
            ON agent_tool_audit_events (invocation_id, sequence)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_tool_audit_confirmation
            ON agent_tool_audit_events (confirmation_id)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_tool_audit_idempotency
            ON agent_tool_audit_events (idempotency_key)
            """
        )
        await db.commit()


async def append_event(
    *,
    invocation_id: str,
    event_type: str,
    tool_name: str,
    sequence: int | None = None,
    event_id: str | None = None,
    occurred_at: str | None = None,
    tool_call_id: str = "",
    invocation_source: str = "",
    actor_user_id: str = "",
    session_target_type: str = "",
    session_target_id: str = "",
    effective_group_id: str = "",
    arguments_fingerprint: str = "",
    confirmation_id: int | None = None,
    confirmation_status: str = "",
    idempotency_key: str = "",
    idempotency_status: str = "",
    risk_level: str = "",
    side_effect: str = "",
    execution_stage: str = "",
    outcome: str = "",
    error_code: str = "",
    retryable: bool = False,
    failure_class: str = "",
    duration_ms: int = 0,
    safe_details: Mapping[str, object] | None = None,
) -> AuditEvent:
    await init_audit_db()
    safe_invocation_id = str(invocation_id).strip()
    safe_event_type = str(event_type).strip()
    safe_tool_name = str(tool_name).strip()
    if not safe_invocation_id or not safe_event_type or not safe_tool_name:
        raise ValueError("invocation_id, event_type, and tool_name are required.")

    safe_event_id = str(event_id or create_invocation_id())
    safe_occurred_at = str(occurred_at or now_text())
    sanitized_details = sanitize_details(safe_details)
    details_json = json.dumps(
        sanitized_details,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        safe_sequence = sequence
        if safe_sequence is None:
            cursor = await db.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) + 1
                FROM agent_tool_audit_events
                WHERE invocation_id = ?
                """,
                (safe_invocation_id,),
            )
            row = await cursor.fetchone()
            safe_sequence = int(row[0] or 1) if row else 1
        safe_sequence = max(1, int(safe_sequence))
        await db.execute(
            """
            INSERT INTO agent_tool_audit_events (
                event_id, invocation_id, sequence, occurred_at, event_type,
                tool_call_id, tool_name, invocation_source,
                actor_user_id, session_target_type, session_target_id, effective_group_id,
                arguments_fingerprint, confirmation_id, confirmation_status,
                idempotency_key, idempotency_status, risk_level, side_effect,
                execution_stage, outcome, error_code, retryable, failure_class,
                duration_ms, safe_details_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                safe_event_id,
                safe_invocation_id,
                safe_sequence,
                safe_occurred_at,
                safe_event_type,
                str(tool_call_id),
                safe_tool_name,
                str(invocation_source),
                str(actor_user_id),
                str(session_target_type),
                str(session_target_id),
                str(effective_group_id),
                str(arguments_fingerprint),
                confirmation_id,
                str(confirmation_status),
                str(idempotency_key),
                str(idempotency_status),
                str(risk_level),
                str(side_effect),
                str(execution_stage),
                str(outcome),
                str(error_code),
                1 if retryable else 0,
                str(failure_class),
                max(0, int(duration_ms)),
                details_json,
            ),
        )
        await db.commit()

    return AuditEvent(
        event_id=safe_event_id,
        invocation_id=safe_invocation_id,
        sequence=safe_sequence,
        occurred_at=safe_occurred_at,
        event_type=safe_event_type,
        tool_call_id=str(tool_call_id),
        tool_name=safe_tool_name,
        invocation_source=str(invocation_source),
        actor_user_id=str(actor_user_id),
        session_target_type=str(session_target_type),
        session_target_id=str(session_target_id),
        effective_group_id=str(effective_group_id),
        arguments_fingerprint=str(arguments_fingerprint),
        confirmation_id=confirmation_id,
        confirmation_status=str(confirmation_status),
        idempotency_key=str(idempotency_key),
        idempotency_status=str(idempotency_status),
        risk_level=str(risk_level),
        side_effect=str(side_effect),
        execution_stage=str(execution_stage),
        outcome=str(outcome),
        error_code=str(error_code),
        retryable=bool(retryable),
        failure_class=str(failure_class),
        duration_ms=max(0, int(duration_ms)),
        safe_details=sanitized_details,
    )
