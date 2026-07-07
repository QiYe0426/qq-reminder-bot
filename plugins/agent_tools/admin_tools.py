from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import aiosqlite

from plugins.access_control import (
    FEATURE_AI_CHAT,
    FEATURE_COLLECTOR,
    FEATURE_COMPANION,
    FEATURE_DAILY_REPORT,
    FEATURE_DAILY_REPORT_AUTO,
    FEATURE_LABELS,
    is_group_feature_enabled,
)
from plugins.message_archive import DB_PATH as ARCHIVE_DB_PATH

from .registry import AgentTool, AgentToolContext, AgentToolResult


MAX_TOOL_REPORT_CHARS = 6000
MAX_PROFILE_MEMORIES = 8


def _tool_definition(
    *,
    name: str,
    description: str,
    properties: dict[str, object],
    required: list[str] | None = None,
) -> dict[str, object]:
    parameters: dict[str, object] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        parameters["required"] = required
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


def current_group_id(context: AgentToolContext) -> str:
    if str(context.get("_target_type") or "") != "group":
        return ""
    return str(context.get("_target_id") or "").strip()


def truncate_text(text: str, limit: int = MAX_TOOL_REPORT_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + "\n...", True


async def group_archive_summary(group_id: str) -> dict[str, object]:
    if not ARCHIVE_DB_PATH.exists():
        return {
            "message_count": 0,
            "last_message_at": "",
            "today_messages": 0,
            "yesterday_messages": 0,
        }

    today = date.today()
    yesterday = today - timedelta(days=1)
    today_start = f"{today.isoformat()} 00:00:00"
    tomorrow_start = f"{(today + timedelta(days=1)).isoformat()} 00:00:00"
    yesterday_start = f"{yesterday.isoformat()} 00:00:00"

    async with aiosqlite.connect(ARCHIVE_DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT COUNT(*), MAX(created_at)
            FROM collected_messages
            WHERE group_id = ?
            """,
            (group_id,),
        )
        total_row = await cursor.fetchone()
        today_cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM collected_messages
            WHERE group_id = ? AND created_at >= ? AND created_at < ?
            """,
            (group_id, today_start, tomorrow_start),
        )
        today_row = await today_cursor.fetchone()
        yesterday_cursor = await db.execute(
            """
            SELECT COUNT(*)
            FROM collected_messages
            WHERE group_id = ? AND created_at >= ? AND created_at < ?
            """,
            (group_id, yesterday_start, today_start),
        )
        yesterday_row = await yesterday_cursor.fetchone()

    return {
        "message_count": int(total_row[0] or 0) if total_row else 0,
        "last_message_at": str(total_row[1] or "") if total_row else "",
        "today_messages": int(today_row[0] or 0) if today_row else 0,
        "yesterday_messages": int(yesterday_row[0] or 0) if yesterday_row else 0,
    }


async def feature_state(group_id: str) -> dict[str, object]:
    feature_names = [
        FEATURE_AI_CHAT,
        FEATURE_COLLECTOR,
        FEATURE_DAILY_REPORT,
        FEATURE_DAILY_REPORT_AUTO,
        FEATURE_COMPANION,
    ]
    features: dict[str, object] = {}
    for feature in feature_names:
        features[feature] = {
            "label": FEATURE_LABELS.get(feature, feature),
            "enabled": await is_group_feature_enabled(group_id, feature),
        }
    return features


async def get_group_status_tool(args: dict[str, object], context: AgentToolContext) -> AgentToolResult:
    group_id = current_group_id(context)
    if not group_id:
        return {"ok": False, "error": "not_group_chat", "message": "群状态只能在群聊中查看。"}

    from plugins.agent_tool_access import group_agent_tool_state
    from plugins.companion_memory import get_group_profile
    from plugins.companion_registry import companion_target_count

    group_profile = await get_group_profile(group_id)
    return {
        "ok": True,
        "group_id": group_id,
        "features": await feature_state(group_id),
        "archive": await group_archive_summary(group_id),
        "companion_target_count": await companion_target_count(group_id),
        "group_profile": group_profile,
        "agent_tools": await group_agent_tool_state(group_id),
        "message": "已读取当前群状态。",
    }


def normalize_report_date_arg(value: object) -> date:
    from plugins.daily_report import parse_report_date

    if value is None or str(value).strip() == "":
        return date.today() - timedelta(days=1)
    return parse_report_date(str(value))


async def generate_daily_report_tool(args: dict[str, object], context: AgentToolContext) -> AgentToolResult:
    group_id = current_group_id(context)
    if not group_id:
        return {"ok": False, "error": "not_group_chat", "message": "日报只能在群聊中生成。"}

    from plugins.daily_report import daily_report_unavailable_reason, generate_ai_daily_report_markdown, report_chat_body

    try:
        target_date = normalize_report_date_arg(args.get("date"))
    except ValueError as exc:
        return {"ok": False, "error": "invalid_date", "message": str(exc)}

    unavailable_reason = await daily_report_unavailable_reason(group_id)
    if unavailable_reason:
        return {"ok": False, "error": "feature_unavailable", "message": unavailable_reason}

    max_chars_arg = args.get("max_chars")
    try:
        max_chars = int(max_chars_arg) if max_chars_arg is not None else MAX_TOOL_REPORT_CHARS
    except (TypeError, ValueError):
        max_chars = MAX_TOOL_REPORT_CHARS
    max_chars = min(max(max_chars, 800), MAX_TOOL_REPORT_CHARS)

    try:
        filename, markdown, preview = await generate_ai_daily_report_markdown(group_id, target_date)
    except Exception as exc:
        return {
            "ok": False,
            "error": "generation_failed",
            "message": f"日报生成失败：{exc}",
        }

    chat_text = report_chat_body(markdown)
    truncated_report, truncated = truncate_text(chat_text, max_chars)
    return {
        "ok": True,
        "group_id": group_id,
        "date": target_date.isoformat(),
        "filename": filename,
        "report": truncated_report,
        "truncated": truncated,
        "preview_chars": len(preview),
        "message": f"已生成 {target_date.isoformat()} 的日报。",
    }


async def get_group_profile_tool(args: dict[str, object], context: AgentToolContext) -> AgentToolResult:
    group_id = current_group_id(context)
    if not group_id:
        return {"ok": False, "error": "not_group_chat", "message": "群画像只能在群聊中查看。"}

    from plugins.companion_memory import get_group_profile

    profile = await get_group_profile(group_id)
    summary = str(profile.get("summary") or "").strip()
    return {
        "ok": True,
        "group_id": group_id,
        "profile": profile,
        "message": summary or "当前群还没有保存群画像。",
    }


async def companion_target_candidates(group_id: str, keyword: str) -> list[dict[str, object]]:
    from plugins.companion_memory import DB_PATH as COMPANION_DB_PATH, init_companion_memory_db
    from plugins.companion_registry import init_companion_db

    await init_companion_db()
    await init_companion_memory_db()
    query = f"%{keyword}%"
    async with aiosqlite.connect(COMPANION_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT target.user_id, target.display_name, target.enabled, target.updated_at
            FROM companion_targets target
            WHERE target.group_id = ?
              AND (
                target.user_id = ?
                OR COALESCE(target.display_name, '') LIKE ?
              )
            ORDER BY target.enabled DESC, target.updated_at DESC
            LIMIT 8
            """,
            (group_id, keyword, query),
        )
        target_rows = await cursor.fetchall()
        if target_rows:
            return [
                {
                    "user_id": str(row["user_id"]),
                    "display_name": str(row["display_name"] or row["user_id"]),
                    "enabled": bool(row["enabled"]),
                    "updated_at": str(row["updated_at"] or ""),
                }
                for row in target_rows
            ]

        cursor = await db.execute(
            """
            SELECT profile.user_id, target.display_name, 1 AS enabled, profile.updated_at
            FROM companion_profiles profile
            LEFT JOIN companion_targets target
              ON target.group_id = profile.group_id AND target.user_id = profile.user_id
            WHERE profile.group_id = ?
              AND profile.user_id = ?
            LIMIT 1
            """,
            (group_id, keyword),
        )
        profile_rows = await cursor.fetchall()
    return [
        {
            "user_id": str(row["user_id"]),
            "display_name": str(row["display_name"] or row["user_id"]),
            "enabled": bool(row["enabled"]),
            "updated_at": str(row["updated_at"] or ""),
        }
        for row in profile_rows
    ]


async def resolve_member_profile_target(group_id: str, args: dict[str, object]) -> tuple[str, list[dict[str, object]]]:
    user_id = str(args.get("user_id") or "").strip()
    if user_id.isdigit():
        return user_id, []
    keyword = str(args.get("keyword") or args.get("display_name") or "").strip()
    if not keyword:
        return "", []
    candidates = await companion_target_candidates(group_id, keyword)
    if len(candidates) == 1:
        return str(candidates[0]["user_id"]), candidates
    return "", candidates


def mentioned_user_ids(context: AgentToolContext) -> list[str]:
    event = context.get("_event")
    get_message = getattr(event, "get_message", None)
    if not callable(get_message):
        return []
    self_id = str(getattr(event, "self_id", "") or "")
    user_ids: list[str] = []
    for segment in get_message():
        if getattr(segment, "type", "") != "at":
            continue
        user_id = str(getattr(segment, "data", {}).get("qq") or "").strip()
        if user_id and user_id != self_id and user_id not in user_ids:
            user_ids.append(user_id)
    return user_ids


def row_to_dict(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


async def get_member_profile_tool(args: dict[str, object], context: AgentToolContext) -> AgentToolResult:
    group_id = current_group_id(context)
    if not group_id:
        return {"ok": False, "error": "not_group_chat", "message": "群友画像只能在群聊中查看。"}

    from plugins.companion_memory import get_profile, lookup_memories, profile_to_text

    if not str(args.get("user_id") or "").strip() and not str(args.get("keyword") or args.get("display_name") or "").strip():
        mentioned = mentioned_user_ids(context)
        if len(mentioned) == 1:
            args = {**args, "user_id": mentioned[0]}
        elif len(mentioned) > 1:
            return {
                "ok": False,
                "error": "ambiguous_target",
                "message": "这条消息里 @ 了多个人，请只 @ 一个要查看画像的人。",
                "candidates": [{"user_id": user_id} for user_id in mentioned],
            }

    user_id, candidates = await resolve_member_profile_target(group_id, args)
    if not user_id:
        return {
            "ok": False,
            "error": "ambiguous_target" if candidates else "missing_target",
            "message": "没有唯一确定要查看谁的画像。请让管理员提供 QQ 号，或在群里 @ 这个人。",
            "candidates": candidates,
        }

    profile = await get_profile(group_id, user_id)
    profile_text = profile_to_text(profile)
    include_memories = bool(args.get("include_memories"))
    memories: list[dict[str, object]] = []
    if include_memories:
        memory_rows = await lookup_memories(
            group_id,
            user_id,
            str(args.get("keyword") or args.get("question") or ""),
            MAX_PROFILE_MEMORIES,
        )
        memories = [row_to_dict(row) or {} for row in memory_rows]

    display_name = ""
    for candidate in candidates:
        if str(candidate.get("user_id") or "") == user_id:
            display_name = str(candidate.get("display_name") or "")
            break
    return {
        "ok": True,
        "group_id": group_id,
        "user_id": user_id,
        "display_name": display_name,
        "profile": row_to_dict(profile),
        "profile_text": profile_text,
        "memories": memories,
        "message": profile_text or "这个群友还没有生成画像。",
    }


ADMIN_TOOLS = [
    AgentTool(
        name="get_group_status",
        category="admin",
        requires_feature=FEATURE_AI_CHAT,
        requires_admin=True,
        requires_group=True,
        definition=_tool_definition(
            name="get_group_status",
            description="Get operational status for the current QQ group, including feature switches, message archive stats, companion targets, group profile, and Agent tool permissions. Admin only.",
            properties={},
        ),
        handler=get_group_status_tool,
    ),
    AgentTool(
        name="generate_daily_report",
        category="daily_report",
        requires_feature=FEATURE_DAILY_REPORT,
        requires_admin=True,
        requires_group=True,
        definition=_tool_definition(
            name="generate_daily_report",
            description="Generate or read the daily report / yesterday summary for the current QQ group. Admin only. Default date is yesterday.",
            properties={
                "date": {
                    "type": "string",
                    "description": "Report date: 昨天, 今天, 前天, or YYYY-MM-DD. Defaults to 昨天.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum returned report characters, capped internally.",
                },
            },
        ),
        handler=generate_daily_report_tool,
    ),
    AgentTool(
        name="get_group_profile",
        category="profile",
        requires_feature=FEATURE_COMPANION,
        requires_admin=True,
        requires_group=True,
        definition=_tool_definition(
            name="get_group_profile",
            description="Get the current group's companion/group profile. Admin only.",
            properties={},
        ),
        handler=get_group_profile_tool,
    ),
    AgentTool(
        name="get_member_profile",
        category="profile",
        requires_feature=FEATURE_COMPANION,
        requires_admin=True,
        requires_group=True,
        definition=_tool_definition(
            name="get_member_profile",
            description="Get a group member's companion profile by QQ user id or exact display-name keyword. Admin only.",
            properties={
                "user_id": {
                    "type": "string",
                    "description": "QQ user id. Prefer this when known.",
                },
                "keyword": {
                    "type": "string",
                    "description": "Display-name keyword when user_id is unknown.",
                },
                "include_memories": {
                    "type": "boolean",
                    "description": "Whether to include a few related long-term memories.",
                },
            },
        ),
        handler=get_member_profile_tool,
    ),
]
