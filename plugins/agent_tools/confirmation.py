from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from .call_identity import arguments_digest, canonical_arguments
from .sensitive_storage import prepare_sensitive_sqlite_path


DB_PATH = Path("data/agent_tool_confirmations.db")

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_EXPIRED = "expired"
STATUS_CONSUMED = "consumed"

CONFIRMATION_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CONFIRMATION_CODE_LENGTH = 8
TOKEN_BYTES = 32


@dataclass(frozen=True)
class ToolConfirmation:
    confirmation_id: int
    confirmation_code: str
    status: str
    tool_name: str
    arguments: dict[str, object]
    arguments_hash: str
    user_id: str
    target_type: str
    target_id: str
    effective_group_id: str
    created_at: str
    expires_at: str
    confirmed_at: str
    consumed_at: str


@dataclass(frozen=True)
class ConfirmationResult:
    ok: bool
    confirmation: ToolConfirmation | None = None
    token: str = ""
    error: str = ""
    message: str = ""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_confirmation_code() -> str:
    return "".join(secrets.choice(CONFIRMATION_CODE_ALPHABET) for _ in range(CONFIRMATION_CODE_LENGTH))


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


async def init_confirmation_db() -> None:
    prepare_sensitive_sqlite_path(DB_PATH)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_tool_confirmations (
                confirmation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                confirmation_code TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                arguments_hash TEXT NOT NULL,
                user_id TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                effective_group_id TEXT NOT NULL,
                token_hash TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                confirmed_at TEXT,
                consumed_at TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_tool_confirmations_code
            ON agent_tool_confirmations (confirmation_code)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_agent_tool_confirmations_token
            ON agent_tool_confirmations (token_hash)
            """
        )
        await db.commit()


def confirmation_from_row(row: aiosqlite.Row | None) -> ToolConfirmation | None:
    if row is None:
        return None
    arguments = json.loads(str(row["arguments_json"]))
    if not isinstance(arguments, dict):
        raise ValueError("Stored confirmation arguments must be a JSON object.")
    return ToolConfirmation(
        confirmation_id=int(row["confirmation_id"]),
        confirmation_code=str(row["confirmation_code"]),
        status=str(row["status"]),
        tool_name=str(row["tool_name"]),
        arguments=arguments,
        arguments_hash=str(row["arguments_hash"]),
        user_id=str(row["user_id"]),
        target_type=str(row["target_type"]),
        target_id=str(row["target_id"]),
        effective_group_id=str(row["effective_group_id"]),
        created_at=str(row["created_at"]),
        expires_at=str(row["expires_at"]),
        confirmed_at=str(row["confirmed_at"] or ""),
        consumed_at=str(row["consumed_at"] or ""),
    )


async def row_by_code(db: aiosqlite.Connection, code: str) -> aiosqlite.Row | None:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM agent_tool_confirmations WHERE confirmation_code = ?",
        (code.upper(),),
    )
    return await cursor.fetchone()


async def row_by_token(db: aiosqlite.Connection, token: str) -> aiosqlite.Row | None:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM agent_tool_confirmations WHERE token_hash = ?",
        (token_digest(token),),
    )
    return await cursor.fetchone()


async def expire_if_needed(db: aiosqlite.Connection, row: aiosqlite.Row) -> aiosqlite.Row:
    if str(row["status"]) not in {STATUS_PENDING, STATUS_CONFIRMED}:
        return row
    if str(row["expires_at"]) > timestamp(now_utc()):
        return row
    await db.execute(
        """
        UPDATE agent_tool_confirmations
        SET status = ?
        WHERE confirmation_id = ? AND status IN (?, ?)
        """,
        (STATUS_EXPIRED, int(row["confirmation_id"]), STATUS_PENDING, STATUS_CONFIRMED),
    )
    await db.commit()
    refreshed = await row_by_code(db, str(row["confirmation_code"]))
    assert refreshed is not None
    return refreshed


def bindings_match(
    confirmation: ToolConfirmation,
    *,
    tool_name: str,
    arguments: dict[str, object],
    user_id: str,
    target_type: str,
    target_id: str,
    effective_group_id: str,
) -> bool:
    return (
        confirmation.tool_name == tool_name
        and confirmation.arguments_hash == arguments_digest(arguments)
        and confirmation.user_id == str(user_id)
        and confirmation.target_type == str(target_type)
        and confirmation.target_id == str(target_id)
        and confirmation.effective_group_id == str(effective_group_id)
    )


async def create_pending_confirmation(
    *,
    tool_name: str,
    arguments: dict[str, object],
    user_id: str,
    target_type: str,
    target_id: str,
    effective_group_id: str,
    timeout_seconds: int,
) -> ToolConfirmation:
    await init_confirmation_db()
    arguments_json = canonical_arguments(arguments)
    digest = arguments_digest(arguments)
    created_at = now_utc()
    expires_at = created_at + timedelta(seconds=max(1, int(timeout_seconds)))

    for _ in range(20):
        code = generate_confirmation_code()
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    """
                    INSERT INTO agent_tool_confirmations (
                        confirmation_code, status, tool_name, arguments_json,
                        arguments_hash, user_id, target_type, target_id,
                        effective_group_id, created_at, expires_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        code,
                        STATUS_PENDING,
                        tool_name,
                        arguments_json,
                        digest,
                        str(user_id),
                        str(target_type),
                        str(target_id),
                        str(effective_group_id),
                        timestamp(created_at),
                        timestamp(expires_at),
                    ),
                )
                await db.commit()
                db.row_factory = aiosqlite.Row
                row_cursor = await db.execute(
                    "SELECT * FROM agent_tool_confirmations WHERE confirmation_id = ?",
                    (int(cursor.lastrowid),),
                )
                confirmation = confirmation_from_row(await row_cursor.fetchone())
                assert confirmation is not None
                return confirmation
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("Unable to generate a unique tool confirmation code.")


async def confirm_pending_confirmation(
    confirmation_code: str,
    *,
    user_id: str,
    target_type: str,
    target_id: str,
) -> ConfirmationResult:
    await init_confirmation_db()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        row = await row_by_code(db, confirmation_code.strip().upper())
        if row is None:
            await db.rollback()
            return ConfirmationResult(False, error="confirmation_not_found", message="确认码不存在。")
        row = await expire_if_needed(db, row)
        confirmation = confirmation_from_row(row)
        assert confirmation is not None
        if confirmation.status == STATUS_EXPIRED:
            return ConfirmationResult(False, confirmation, error="confirmation_expired", message="确认请求已过期。")
        if confirmation.status != STATUS_PENDING:
            await db.rollback()
            return ConfirmationResult(False, confirmation, error="confirmation_not_pending", message="确认请求已经失效。")
        if (
            confirmation.user_id != str(user_id)
            or confirmation.target_type != str(target_type)
            or confirmation.target_id != str(target_id)
        ):
            await db.rollback()
            return ConfirmationResult(False, confirmation, error="confirmation_scope_mismatch", message="确认请求不属于当前用户或会话。")

        token = generate_token()
        confirmed_at = timestamp(now_utc())
        cursor = await db.execute(
            """
            UPDATE agent_tool_confirmations
            SET status = ?, token_hash = ?, confirmed_at = ?
            WHERE confirmation_id = ? AND status = ? AND expires_at > ?
            """,
            (
                STATUS_CONFIRMED,
                token_digest(token),
                confirmed_at,
                confirmation.confirmation_id,
                STATUS_PENDING,
                confirmed_at,
            ),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            return ConfirmationResult(False, confirmation, error="confirmation_expired", message="确认请求已过期。")
        await db.commit()
        refreshed = confirmation_from_row(await row_by_code(db, confirmation.confirmation_code))
        return ConfirmationResult(True, refreshed, token=token)


async def validate_confirmation(
    token: str,
    *,
    tool_name: str,
    arguments: dict[str, object],
    user_id: str,
    target_type: str,
    target_id: str,
    effective_group_id: str,
) -> ConfirmationResult:
    await init_confirmation_db()
    if not token:
        return ConfirmationResult(False, error="confirmation_invalid", message="缺少内部确认凭证。")
    async with aiosqlite.connect(DB_PATH) as db:
        row = await row_by_token(db, token)
        if row is None:
            return ConfirmationResult(False, error="confirmation_invalid", message="确认凭证无效。")
        row = await expire_if_needed(db, row)
        confirmation = confirmation_from_row(row)
        assert confirmation is not None
        if confirmation.status == STATUS_EXPIRED:
            return ConfirmationResult(False, confirmation, error="confirmation_expired", message="确认凭证已过期。")
        if confirmation.status == STATUS_CONSUMED:
            return ConfirmationResult(False, confirmation, error="confirmation_consumed", message="确认凭证已经使用。")
        if confirmation.status != STATUS_CONFIRMED:
            return ConfirmationResult(False, confirmation, error="confirmation_invalid", message="确认凭证尚未生效。")
        if not bindings_match(
            confirmation,
            tool_name=tool_name,
            arguments=arguments,
            user_id=user_id,
            target_type=target_type,
            target_id=target_id,
            effective_group_id=effective_group_id,
        ):
            return ConfirmationResult(False, confirmation, error="confirmation_mismatch", message="确认凭证与当前工具调用不匹配。")
        return ConfirmationResult(True, confirmation)


async def consume_confirmation(
    token: str,
    *,
    tool_name: str,
    arguments: dict[str, object],
    user_id: str,
    target_type: str,
    target_id: str,
    effective_group_id: str,
) -> ConfirmationResult:
    await init_confirmation_db()
    if not token:
        return ConfirmationResult(False, error="confirmation_invalid", message="缺少内部确认凭证。")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        row = await row_by_token(db, token)
        if row is None:
            await db.rollback()
            return ConfirmationResult(False, error="confirmation_invalid", message="确认凭证无效。")
        row = await expire_if_needed(db, row)
        confirmation = confirmation_from_row(row)
        assert confirmation is not None
        if confirmation.status == STATUS_EXPIRED:
            return ConfirmationResult(False, confirmation, error="confirmation_expired", message="确认凭证已过期。")
        if confirmation.status == STATUS_CONSUMED:
            await db.rollback()
            return ConfirmationResult(False, confirmation, error="confirmation_consumed", message="确认凭证已经使用。")
        if confirmation.status != STATUS_CONFIRMED or not bindings_match(
            confirmation,
            tool_name=tool_name,
            arguments=arguments,
            user_id=user_id,
            target_type=target_type,
            target_id=target_id,
            effective_group_id=effective_group_id,
        ):
            await db.rollback()
            return ConfirmationResult(False, confirmation, error="confirmation_mismatch", message="确认凭证与当前工具调用不匹配。")

        consumed_at = timestamp(now_utc())
        cursor = await db.execute(
            """
            UPDATE agent_tool_confirmations
            SET status = ?, consumed_at = ?
            WHERE confirmation_id = ? AND status = ? AND token_hash = ? AND expires_at > ?
            """,
            (
                STATUS_CONSUMED,
                consumed_at,
                confirmation.confirmation_id,
                STATUS_CONFIRMED,
                token_digest(token),
                consumed_at,
            ),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            return ConfirmationResult(False, confirmation, error="confirmation_consumed", message="确认凭证已经使用。")
        await db.commit()
        refreshed = confirmation_from_row(await row_by_code(db, confirmation.confirmation_code))
        return ConfirmationResult(True, refreshed)
