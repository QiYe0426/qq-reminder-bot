from __future__ import annotations

import asyncio
import os
import re
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv
from nonebot import get_bot, get_driver, on_message
from nonebot.adapters.onebot.v11 import Event, Message, PrivateMessageEvent
from nonebot.log import logger
from nonebot.rule import Rule

from plugins.access_control import parse_id_set


load_dotenv(".env.local")

driver = get_driver()

DB_PATH = Path("data/remote_approvals.db")
ROUTE_PREFIX = "/hunterbot/remote-approval"
API_PREFIX = f"{ROUTE_PREFIX}/api"

REMOTE_APPROVAL_ENABLED_ENV = "REMOTE_APPROVAL_ENABLED"
REMOTE_APPROVAL_USER_ID_ENV = "REMOTE_APPROVAL_USER_ID"
REMOTE_APPROVAL_API_TOKEN_ENV = "REMOTE_APPROVAL_API_TOKEN"
REMOTE_APPROVAL_TOKEN_FALLBACK_ENVS = ("CODEX_REMOTE_APPROVAL_TOKEN", "COMPANION_ADMIN_TOKEN")
REMOTE_APPROVAL_USER_FALLBACK_ENVS = ("BOT_ADMIN_USER_IDS", "BOT_OWNER_USER_IDS", "OWNER_USER_IDS")

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"
STATUS_CANCELLED = "cancelled"

APPROVAL_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
APPROVAL_CODE_LENGTH = 6

DECISION_APPROVE = "approve"
DECISION_DENY = "deny"
DECISION_CANCEL = "cancel"

DECISION_WORDS = {
    "批准": DECISION_APPROVE,
    "同意": DECISION_APPROVE,
    "通过": DECISION_APPROVE,
    "approve": DECISION_APPROVE,
    "allow": DECISION_APPROVE,
    "yes": DECISION_APPROVE,
    "y": DECISION_APPROVE,
    "拒绝": DECISION_DENY,
    "驳回": DECISION_DENY,
    "否决": DECISION_DENY,
    "deny": DECISION_DENY,
    "reject": DECISION_DENY,
    "no": DECISION_DENY,
    "n": DECISION_DENY,
    "退出审批": DECISION_CANCEL,
    "取消审批": DECISION_CANCEL,
    "停止审批": DECISION_CANCEL,
    "退出": DECISION_CANCEL,
    "cancel": DECISION_CANCEL,
    "abort": DECISION_CANCEL,
    "quit": DECISION_CANCEL,
    "exit": DECISION_CANCEL,
}

STATUS_COMMANDS = {"审批状态", "远程审批", "审批列表", "remote approval", "remote approval status"}

DECISION_RE = re.compile(
    r"^(退出审批|取消审批|停止审批|批准|同意|通过|拒绝|驳回|否决|退出|approve|allow|yes|y|deny|reject|no|n|cancel|abort|quit|exit)"
    r"(?:\s+([A-Za-z0-9-]{4,20}))?\s*$",
    re.IGNORECASE,
)

_db_ready = False

try:
    from fastapi import Header, HTTPException, Query, Request
    from fastapi.responses import JSONResponse
except Exception:  # pragma: no cover
    Header = None
    HTTPException = None
    Query = None
    Request = None
    JSONResponse = None


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_bool(value: str | None, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def remote_approval_enabled() -> bool:
    return normalize_bool(os.getenv(REMOTE_APPROVAL_ENABLED_ENV), True)


def configured_remote_user_id() -> str:
    configured = os.getenv(REMOTE_APPROVAL_USER_ID_ENV, "").strip()
    configured_ids = parse_id_set(configured)
    if len(configured_ids) == 1:
        return next(iter(configured_ids))
    if len(configured_ids) > 1:
        return ""

    fallback_ids: set[str] = set()
    for env_name in REMOTE_APPROVAL_USER_FALLBACK_ENVS:
        fallback_ids.update(parse_id_set(os.getenv(env_name, "")))
    if len(fallback_ids) == 1:
        return next(iter(fallback_ids))
    return ""


def configured_api_token() -> str:
    token = os.getenv(REMOTE_APPROVAL_API_TOKEN_ENV, "").strip()
    if token:
        return token
    for env_name in REMOTE_APPROVAL_TOKEN_FALLBACK_ENVS:
        token = os.getenv(env_name, "").strip()
        if token:
            return token
    return ""


def is_allowed_remote_user(user_id: str | int) -> bool:
    expected = configured_remote_user_id()
    return bool(expected and str(user_id) == expected)


def safe_text(value: object, limit: int = 600) -> str:
    text = " ".join(str(value or "").split()).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def generate_request_id() -> str:
    return secrets.token_urlsafe(18)


def generate_approval_code() -> str:
    return "".join(secrets.choice(APPROVAL_CODE_ALPHABET) for _ in range(APPROVAL_CODE_LENGTH))


def parse_decision_text(text: str) -> tuple[str, str | None] | str | None:
    normalized = " ".join(text.strip().split())
    if not normalized:
        return None
    if normalized.lower() in STATUS_COMMANDS or normalized in STATUS_COMMANDS:
        return "status"
    match = DECISION_RE.match(normalized)
    if not match:
        return None
    word = match.group(1).lower()
    decision = DECISION_WORDS.get(word) or DECISION_WORDS.get(match.group(1))
    if decision is None:
        return None
    code = match.group(2).upper() if match.group(2) else None
    return decision, code


async def init_remote_approval_db() -> None:
    global _db_ready
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS remote_approval_requests (
                request_id TEXT PRIMARY KEY,
                approval_code TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                tool_name TEXT NOT NULL DEFAULT '',
                operation_summary TEXT NOT NULL DEFAULT '',
                cwd_summary TEXT NOT NULL DEFAULT '',
                reason_summary TEXT NOT NULL DEFAULT '',
                risk_summary TEXT NOT NULL DEFAULT '',
                requester TEXT NOT NULL DEFAULT '',
                payload_digest TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                notified_at TEXT,
                decided_at TEXT,
                decision_user_id TEXT,
                decision_message TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_remote_approval_status_created_at
            ON remote_approval_requests (status, created_at)
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_remote_approval_code
            ON remote_approval_requests (approval_code)
            """
        )
        await db.commit()
    _db_ready = True


async def ensure_remote_approval_db() -> None:
    if not _db_ready:
        await init_remote_approval_db()


async def row_by_request_id(request_id: str) -> aiosqlite.Row | None:
    await ensure_remote_approval_db()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT *
            FROM remote_approval_requests
            WHERE request_id = ?
            """,
            (request_id,),
        )
        return await cursor.fetchone()


async def row_by_code(code: str) -> aiosqlite.Row | None:
    await ensure_remote_approval_db()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT *
            FROM remote_approval_requests
            WHERE approval_code = ?
            """,
            (code.upper(),),
        )
        return await cursor.fetchone()


def request_to_dict(row: aiosqlite.Row | None) -> dict[str, object] | None:
    if row is None:
        return None
    return {
        "request_id": row["request_id"],
        "approval_code": row["approval_code"],
        "status": row["status"],
        "tool_name": row["tool_name"],
        "operation_summary": row["operation_summary"],
        "cwd_summary": row["cwd_summary"],
        "reason_summary": row["reason_summary"],
        "risk_summary": row["risk_summary"],
        "requester": row["requester"],
        "payload_digest": row["payload_digest"],
        "created_at": row["created_at"],
        "notified_at": row["notified_at"],
        "decided_at": row["decided_at"],
        "decision_user_id": row["decision_user_id"],
        "decision_message": row["decision_message"],
    }


def normalized_request_payload(payload: dict[str, object]) -> dict[str, str]:
    return {
        "tool_name": safe_text(payload.get("tool_name"), 120),
        "operation_summary": safe_text(payload.get("operation_summary") or payload.get("summary"), 700),
        "cwd_summary": safe_text(payload.get("cwd_summary") or payload.get("cwd"), 280),
        "reason_summary": safe_text(payload.get("reason_summary") or payload.get("reason"), 500),
        "risk_summary": safe_text(payload.get("risk_summary") or payload.get("risk"), 300),
        "requester": safe_text(payload.get("requester"), 120),
        "payload_digest": safe_text(payload.get("payload_digest"), 80),
    }


async def create_remote_approval_request(payload: dict[str, object]) -> dict[str, object]:
    await ensure_remote_approval_db()
    normalized = normalized_request_payload(payload)
    if not normalized["operation_summary"]:
        normalized["operation_summary"] = "Codex 请求执行一项需要审批的操作，摘要为空。"
    timestamp = now_text()

    for _ in range(20):
        request_id = generate_request_id()
        approval_code = generate_approval_code()
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    """
                    INSERT INTO remote_approval_requests (
                        request_id,
                        approval_code,
                        status,
                        tool_name,
                        operation_summary,
                        cwd_summary,
                        reason_summary,
                        risk_summary,
                        requester,
                        payload_digest,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        approval_code,
                        STATUS_PENDING,
                        normalized["tool_name"],
                        normalized["operation_summary"],
                        normalized["cwd_summary"],
                        normalized["reason_summary"],
                        normalized["risk_summary"],
                        normalized["requester"],
                        normalized["payload_digest"],
                        timestamp,
                    ),
                )
                await db.commit()
            row = await row_by_request_id(request_id)
            result = request_to_dict(row)
            if result is None:
                raise RuntimeError("failed to create remote approval request")
            return result
        except sqlite3.IntegrityError:
            continue

    raise RuntimeError("failed to generate a unique remote approval code")


def approval_message(row: dict[str, object]) -> str:
    code = str(row["approval_code"])
    lines = [
        "Codex 远程审批请求",
        f"审批码：{code}",
    ]
    tool_name = safe_text(row.get("tool_name"), 80)
    if tool_name:
        lines.append(f"工具：{tool_name}")
    cwd_summary = safe_text(row.get("cwd_summary"), 220)
    if cwd_summary:
        lines.append(f"目录：{cwd_summary}")
    lines.append(f"摘要：{safe_text(row.get('operation_summary'), 520)}")
    reason = safe_text(row.get("reason_summary"), 360)
    if reason:
        lines.append(f"原因：{reason}")
    risk = safe_text(row.get("risk_summary"), 220)
    if risk:
        lines.append(f"风险：{risk}")
    digest = safe_text(row.get("payload_digest"), 80)
    if digest:
        lines.append(f"摘要指纹：{digest}")
    lines.extend(
        [
            "",
            f"批准：批准 {code}",
            f"拒绝：拒绝 {code}",
            f"退出本次远程审批：退出审批 {code}",
            "提示：这里不会发送完整命令、密钥、token 或环境变量。",
        ]
    )
    return "\n".join(lines)


async def mark_notified(request_id: str) -> None:
    await ensure_remote_approval_db()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE remote_approval_requests
            SET notified_at = ?
            WHERE request_id = ?
            """,
            (now_text(), request_id),
        )
        await db.commit()


async def notify_remote_owner(row: dict[str, object]) -> bool:
    user_id = configured_remote_user_id()
    if not user_id:
        return False
    try:
        bot = get_bot()
    except Exception:
        logger.warning("No OneBot connection is active; skip remote approval notification.")
        return False

    try:
        await bot.call_api("send_private_msg", user_id=int(user_id), message=approval_message(row))
    except Exception:
        logger.exception("Failed to send remote approval QQ notification.")
        return False

    await mark_notified(str(row["request_id"]))
    return True


async def pending_requests(limit: int = 8) -> list[dict[str, object]]:
    await ensure_remote_approval_db()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT *
            FROM remote_approval_requests
            WHERE status = ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (STATUS_PENDING, limit),
        )
        rows = await cursor.fetchall()
    return [item for row in rows if (item := request_to_dict(row))]


async def resolve_pending_code(provided_code: str | None) -> tuple[str | None, str | None]:
    if provided_code:
        return provided_code.upper(), None
    rows = await pending_requests(limit=10)
    if not rows:
        return None, "当前没有等待中的远程审批。"
    if len(rows) > 1:
        codes = "、".join(str(row["approval_code"]) for row in rows)
        return None, f"当前有多个等待中的审批，请带上审批码：{codes}"
    return str(rows[0]["approval_code"]), None


async def decide_request_by_code(code: str, decision: str, user_id: str | int) -> str:
    row = await row_by_code(code)
    if row is None:
        return f"没有找到审批码 {code}。"
    if row["status"] != STATUS_PENDING:
        return f"审批码 {code} 已失效，当前状态：{row['status']}。"

    if decision == DECISION_APPROVE:
        status = STATUS_APPROVED
        message = "Approved from QQ remote approval."
        reply = f"已批准 {code}，这次审批码已失效。"
    elif decision == DECISION_DENY:
        status = STATUS_DENIED
        message = "Denied from QQ remote approval."
        reply = f"已拒绝 {code}，这次审批码已失效。"
    else:
        status = STATUS_CANCELLED
        message = "Cancelled from QQ remote approval."
        reply = f"已退出本次远程审批 {code}，Codex 会收到取消结果。"

    await ensure_remote_approval_db()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE remote_approval_requests
            SET status = ?,
                decided_at = ?,
                decision_user_id = ?,
                decision_message = ?
            WHERE approval_code = ? AND status = ?
            """,
            (status, now_text(), str(user_id), message, code, STATUS_PENDING),
        )
        await db.commit()
    return reply


def pending_status_text(rows: list[dict[str, object]]) -> str:
    if not rows:
        return "当前没有等待中的远程审批。"
    lines = ["等待中的 Codex 远程审批："]
    for row in rows:
        lines.append(
            f"- {row['approval_code']}：{safe_text(row.get('operation_summary'), 90)}"
            + (f"（{safe_text(row.get('created_at'), 20)}）" if row.get("created_at") else "")
        )
    lines.append("回复：批准 审批码 / 拒绝 审批码 / 退出审批 审批码")
    return "\n".join(lines)


async def remote_approval_rule(event: Event) -> bool:
    if not isinstance(event, PrivateMessageEvent):
        return False
    return parse_decision_text(event.get_plaintext()) is not None


remote_approval_message = on_message(rule=Rule(remote_approval_rule), priority=2, block=True)


@driver.on_startup
async def startup_remote_approval() -> None:
    await init_remote_approval_db()


@remote_approval_message.handle()
async def handle_remote_approval_message(event: PrivateMessageEvent) -> None:
    parsed = parse_decision_text(event.get_plaintext())
    if parsed is None:
        return
    if not remote_approval_enabled():
        await remote_approval_message.finish(Message("远程审批插件已关闭。"))
    if not configured_remote_user_id():
        await remote_approval_message.finish(Message(f"远程审批未配置，请设置 {REMOTE_APPROVAL_USER_ID_ENV}=你的QQ号。"))
    if not is_allowed_remote_user(event.user_id):
        await remote_approval_message.finish(Message("这个远程审批入口只允许配置的 QQ 号使用。"))

    if parsed == "status":
        await remote_approval_message.finish(Message(pending_status_text(await pending_requests())))

    decision, provided_code = parsed
    code, error = await resolve_pending_code(provided_code)
    if error:
        await remote_approval_message.finish(Message(error))
    assert code is not None
    await remote_approval_message.finish(Message(await decide_request_by_code(code, decision, event.user_id)))


server_app = getattr(driver, "server_app", None)
if (
    server_app is not None
    and HTTPException is not None
    and Header is not None
    and Query is not None
    and Request is not None
    and JSONResponse is not None
):

    def check_remote_approval_ready() -> None:
        if not remote_approval_enabled():
            raise HTTPException(status_code=403, detail="remote approval is disabled")
        if not configured_remote_user_id():
            raise HTTPException(status_code=403, detail=f"{REMOTE_APPROVAL_USER_ID_ENV} is not configured")
        if not configured_api_token():
            raise HTTPException(status_code=403, detail=f"{REMOTE_APPROVAL_API_TOKEN_ENV} is not configured")

    def check_api_token(token: str | None = None, authorization: str | None = None) -> None:
        check_remote_approval_ready()
        expected = configured_api_token()
        provided = (token or "").strip()
        if not provided and authorization:
            scheme, _, value = authorization.partition(" ")
            if scheme.lower() == "bearer":
                provided = value.strip()
        if provided != expected:
            raise HTTPException(status_code=401, detail="invalid remote approval token")

    @server_app.post(f"{API_PREFIX}/requests")
    async def remote_approval_create_request(
        request: Request,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_api_token(token=token, authorization=authorization)
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="request payload must be a JSON object")
        row = await create_remote_approval_request(payload)
        notified = await notify_remote_owner(row)
        return JSONResponse({"request": row, "notified": notified})

    @server_app.get(f"{API_PREFIX}/requests/{{request_id}}")
    async def remote_approval_get_request(
        request_id: str,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_api_token(token=token, authorization=authorization)
        row = request_to_dict(await row_by_request_id(request_id))
        if row is None:
            raise HTTPException(status_code=404, detail="remote approval request not found")
        return JSONResponse({"request": row})

    @server_app.get(f"{API_PREFIX}/requests/{{request_id}}/wait")
    async def remote_approval_wait_request(
        request_id: str,
        wait_seconds: int = Query(default=25),
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_api_token(token=token, authorization=authorization)
        safe_wait_seconds = min(max(int(wait_seconds or 25), 1), 30)
        deadline = asyncio.get_running_loop().time() + safe_wait_seconds
        while True:
            row = request_to_dict(await row_by_request_id(request_id))
            if row is None:
                raise HTTPException(status_code=404, detail="remote approval request not found")
            if row["status"] != STATUS_PENDING:
                return JSONResponse({"request": row})
            if asyncio.get_running_loop().time() >= deadline:
                return JSONResponse({"request": row})
            await asyncio.sleep(0.5)

    @server_app.get(f"{API_PREFIX}/pending")
    async def remote_approval_pending(
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_api_token(token=token, authorization=authorization)
        return JSONResponse({"requests": await pending_requests(limit=20)})
