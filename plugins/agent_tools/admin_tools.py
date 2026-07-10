from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import aiosqlite
from nonebot.log import logger

from plugins.access_control import (
    DB_PATH as ACCESS_DB_PATH,
    FEATURE_AI_CHAT,
    FEATURE_BOT_TEASE,
    FEATURE_COLLECTOR,
    FEATURE_COMPANION,
    FEATURE_CONSTANT_RETORT,
    FEATURE_DAILY_REPORT,
    FEATURE_DAILY_REPORT_AUTO,
    FEATURE_KEYWORD_RETORT,
    FEATURE_LABELS,
    enforce_group_feature_dependencies,
    is_group_feature_enabled,
    normalize_feature_name,
    set_group_feature,
    set_group_feature_limits,
)
from plugins.message_archive import DB_PATH as ARCHIVE_DB_PATH

from .registry import AgentTool, AgentToolContext, AgentToolResult


MAX_TOOL_REPORT_CHARS = 6000
MAX_PROFILE_MEMORIES = 8
ADMIN_CONFIGURABLE_FEATURES = [
    FEATURE_AI_CHAT,
    FEATURE_COLLECTOR,
    FEATURE_DAILY_REPORT,
    FEATURE_DAILY_REPORT_AUTO,
    FEATURE_COMPANION,
    FEATURE_BOT_TEASE,
    FEATURE_CONSTANT_RETORT,
    FEATURE_KEYWORD_RETORT,
]


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


def target_group_id(args: dict[str, object], context: AgentToolContext) -> str:
    del args
    return str(context.get("_effective_group_id") or "").strip() or current_group_id(context)


def missing_group_message() -> str:
    return "请提供要操作的群号，例如 group_id=722290838。"


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


async def latest_daily_report_runs(group_id: str, limit: int = 5) -> list[dict[str, object]]:
    from plugins.daily_report import init_daily_report_run_db

    await init_daily_report_run_db()
    async with aiosqlite.connect(ACCESS_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT group_id, target_date, status, error, updated_at
            FROM daily_report_runs
            WHERE group_id = ?
            ORDER BY target_date DESC, updated_at DESC
            LIMIT ?
            """,
            (group_id, max(1, min(int(limit or 5), 20))),
        )
        rows = await cursor.fetchall()
    return [row_to_dict(row) or {} for row in rows]


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
    group_id = target_group_id(args, context)
    if not group_id:
        return {"ok": False, "error": "missing_group_id", "message": missing_group_message()}

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
        "daily_report_runs": await latest_daily_report_runs(group_id),
        "message": f"已读取群 {group_id} 状态。",
    }


def normalize_report_date_arg(value: object) -> date:
    from plugins.daily_report import parse_report_date

    if value is None or str(value).strip() == "":
        return date.today() - timedelta(days=1)
    return parse_report_date(str(value))


async def send_tool_progress(context: AgentToolContext, message: str) -> None:
    try:
        from nonebot import get_bot

        bot = get_bot()
        if current_group_id(context):
            await bot.send_group_msg(group_id=int(current_group_id(context)), message=message)
            return

        user_id = str(context.get("_user_id") or "").strip()
        if user_id.isdigit():
            await bot.send_private_msg(user_id=int(user_id), message=message)
    except Exception:
        logger.exception("Failed to send agent tool progress message")


async def generate_daily_report_tool(args: dict[str, object], context: AgentToolContext) -> AgentToolResult:
    group_id = target_group_id(args, context)
    if not group_id:
        return {"ok": False, "error": "missing_group_id", "message": missing_group_message()}

    from plugins.daily_report import (
        daily_report_unavailable_reason,
        find_existing_daily_report_files,
        generate_ai_daily_report_markdown,
        mark_daily_report_run,
        report_chat_body,
    )

    try:
        target_date = normalize_report_date_arg(args.get("date"))
    except ValueError as exc:
        return {"ok": False, "error": "invalid_date", "message": str(exc)}

    unavailable_reason = await daily_report_unavailable_reason(group_id)
    if unavailable_reason:
        await mark_daily_report_run(group_id, target_date, "failed", unavailable_reason)
        return {"ok": False, "error": "feature_unavailable", "message": unavailable_reason}

    max_chars_arg = args.get("max_chars")
    try:
        max_chars = int(max_chars_arg) if max_chars_arg is not None else MAX_TOOL_REPORT_CHARS
    except (TypeError, ValueError):
        max_chars = MAX_TOOL_REPORT_CHARS
    max_chars = min(max(max_chars, 800), MAX_TOOL_REPORT_CHARS)

    existing_files = find_existing_daily_report_files(group_id, target_date)
    if existing_files:
        filename, markdown, pdf_filename, image_filename = existing_files
        if markdown:
            await mark_daily_report_run(group_id, target_date, "sent")
            await send_tool_progress(
                context,
                f"找到群 {group_id} {target_date.isoformat()} 已生成的日报，直接读取，不重新调用 AI。",
            )
            chat_text = report_chat_body(markdown)
            truncated_report, truncated = truncate_text(chat_text, max_chars)
            return {
                "ok": True,
                "group_id": group_id,
                "date": target_date.isoformat(),
                "filename": filename,
                "image_filename": image_filename,
                "pdf_filename": pdf_filename,
                "report": truncated_report,
                "truncated": truncated,
                "preview_chars": len(markdown),
                "reused_existing": True,
                "message": f"已读取 {target_date.isoformat()} 的已生成日报。",
            }

    await mark_daily_report_run(group_id, target_date, "running")
    await send_tool_progress(
        context,
        f"开始生成群 {group_id} {target_date.isoformat()} 的日报，可能要等几十秒。",
    )

    try:
        filename, markdown, preview = await generate_ai_daily_report_markdown(group_id, target_date)
    except Exception as exc:
        error_text = repr(exc)
        await mark_daily_report_run(group_id, target_date, "failed", error_text)
        await send_tool_progress(context, f"日报生成失败：{error_text[:500]}")
        return {
            "ok": False,
            "error": "generation_failed",
            "message": f"日报生成失败：{exc}",
        }

    await mark_daily_report_run(group_id, target_date, "sent")
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
        "reused_existing": False,
        "message": f"已生成 {target_date.isoformat()} 的日报。",
    }


async def get_group_profile_tool(args: dict[str, object], context: AgentToolContext) -> AgentToolResult:
    group_id = target_group_id(args, context)
    if not group_id:
        return {"ok": False, "error": "missing_group_id", "message": missing_group_message()}

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
    group_id = target_group_id(args, context)
    if not group_id:
        return {"ok": False, "error": "missing_group_id", "message": missing_group_message()}

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


def requested_feature_updates(args: dict[str, object]) -> dict[str, bool]:
    updates: dict[str, bool] = {}
    for feature in ADMIN_CONFIGURABLE_FEATURES:
        if feature in args and isinstance(args[feature], bool):
            updates[feature] = bool(args[feature])
    aliases = args.get("features")
    if isinstance(aliases, dict):
        for raw_name, value in aliases.items():
            feature = normalize_feature_name(str(raw_name)) or str(raw_name).strip()
            if feature in ADMIN_CONFIGURABLE_FEATURES and isinstance(value, bool):
                updates[feature] = bool(value)
    return updates


async def apply_feature_limit_updates(group_id: str, args: dict[str, object]) -> list[dict[str, object]]:
    raw_limits = args.get("limits")
    if not isinstance(raw_limits, dict):
        return []

    updated: list[dict[str, object]] = []
    for raw_feature, values in raw_limits.items():
        feature = normalize_feature_name(str(raw_feature)) or str(raw_feature).strip()
        if feature not in {FEATURE_BOT_TEASE, FEATURE_CONSTANT_RETORT, FEATURE_KEYWORD_RETORT}:
            continue
        if not isinstance(values, dict):
            continue
        await set_group_feature_limits(group_id, feature, values)
        updated.append({"feature": feature, "label": FEATURE_LABELS.get(feature, feature)})
    return updated


async def set_group_features_tool(args: dict[str, object], context: AgentToolContext) -> AgentToolResult:
    group_id = target_group_id(args, context)
    if not group_id:
        return {"ok": False, "error": "missing_group_id", "message": missing_group_message()}

    updates = requested_feature_updates(args)
    has_limit_updates = isinstance(args.get("limits"), dict)
    if not updates and not has_limit_updates:
        return {
            "ok": False,
            "error": "missing_changes",
            "message": "请说明要开启/关闭哪些功能，或提供 limits 调整限流。",
        }

    current = {feature: await is_group_feature_enabled(group_id, feature) for feature in ADMIN_CONFIGURABLE_FEATURES}
    desired = {**current, **updates}
    if desired.get(FEATURE_DAILY_REPORT) and not desired.get(FEATURE_COLLECTOR):
        return {
            "ok": False,
            "error": "dependency_failed",
            "message": "日报依赖消息采集。请同时开启消息采集，或先开启消息采集后再开启日报。",
        }
    if desired.get(FEATURE_COMPANION) and not desired.get(FEATURE_COLLECTOR):
        return {
            "ok": False,
            "error": "dependency_failed",
            "message": "陪伴画像依赖消息采集。请同时开启消息采集，或先开启消息采集后再开启陪伴画像。",
        }
    if desired.get(FEATURE_DAILY_REPORT_AUTO) and not desired.get(FEATURE_DAILY_REPORT):
        return {
            "ok": False,
            "error": "dependency_failed",
            "message": "自动发送日报依赖日报功能。请同时开启日报，或先开启日报后再开启自动发送日报。",
        }

    for feature in ADMIN_CONFIGURABLE_FEATURES:
        if feature in updates:
            await set_group_feature(group_id, feature, updates[feature])
    await enforce_group_feature_dependencies(group_id)
    limit_updates = await apply_feature_limit_updates(group_id, args)
    if not updates and not limit_updates:
        return {
            "ok": False,
            "error": "missing_changes",
            "message": "没有识别到可调整的限流功能。",
        }

    return {
        "ok": True,
        "group_id": group_id,
        "updated_features": [
            {
                "feature": feature,
                "label": FEATURE_LABELS.get(feature, feature),
                "enabled": enabled,
            }
            for feature, enabled in updates.items()
        ],
        "updated_limits": limit_updates,
        "features": await feature_state(group_id),
        "message": f"已更新群 {group_id} 的功能配置。",
    }


ADMIN_TOOLS = [
    AgentTool(
        name="get_group_status",
        category="admin",
        requires_feature=FEATURE_AI_CHAT,
        requires_admin=True,
        group_scope="private_explicit",
        requires_target_group_admin=True,
        definition=_tool_definition(
            name="get_group_status",
            description="Get operational status for a QQ group, including feature switches, message archive stats, daily report runs, companion targets, group profile, and Agent tool permissions. Admin only. In private chat, group_id is required.",
            properties={
                "group_id": {
                    "type": "string",
                    "description": "QQ group id. Required in private chat; optional in a group chat.",
                }
            },
        ),
        handler=get_group_status_tool,
    ),
    AgentTool(
        name="generate_daily_report",
        category="daily_report",
        requires_feature=FEATURE_DAILY_REPORT,
        requires_admin=True,
        group_scope="private_explicit",
        requires_target_group_admin=True,
        side_effect="external",
        risk_level="high",
        requires_confirmation=True,
        confirmation_timeout=180,
        idempotency_enabled=True,
        idempotency_ttl=1800,
        idempotency_lease_timeout=300,
        idempotency_temporary_failure_ttl=30,
        idempotency_temporary_errors=frozenset({"generation_failed", "feature_unavailable"}),
        definition=_tool_definition(
            name="generate_daily_report",
            description="Generate or read the daily report / yesterday summary for a QQ group. Admin only. Default date is yesterday. In private chat, group_id is required. Sends progress feedback before generation and records failed/running/sent status.",
            properties={
                "group_id": {
                    "type": "string",
                    "description": "QQ group id. Required in private chat; optional in a group chat.",
                },
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
        group_scope="private_explicit",
        requires_target_group_admin=True,
        definition=_tool_definition(
            name="get_group_profile",
            description="Get a QQ group's companion/group profile. Admin only. In private chat, group_id is required.",
            properties={
                "group_id": {
                    "type": "string",
                    "description": "QQ group id. Required in private chat; optional in a group chat.",
                }
            },
        ),
        handler=get_group_profile_tool,
    ),
    AgentTool(
        name="get_member_profile",
        category="profile",
        requires_feature=FEATURE_COMPANION,
        requires_admin=True,
        group_scope="private_explicit",
        requires_target_group_admin=True,
        definition=_tool_definition(
            name="get_member_profile",
            description="Get a group member's companion profile by QQ user id or exact display-name keyword. Admin only. In private chat, group_id is required.",
            properties={
                "group_id": {
                    "type": "string",
                    "description": "QQ group id. Required in private chat; optional in a group chat.",
                },
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
    AgentTool(
        name="set_group_features",
        category="admin",
        requires_admin=True,
        group_scope="private_explicit",
        requires_target_group_admin=True,
        side_effect="write",
        risk_level="high",
        requires_confirmation=True,
        confirmation_timeout=120,
        idempotency_enabled=True,
        idempotency_ttl=120,
        idempotency_lease_timeout=30,
        definition=_tool_definition(
            name="set_group_features",
            description="Open, close, or adjust feature switches for a QQ group. Admin only. In private chat, group_id is required. Daily report and companion depend on collector; daily_report_auto depends on daily_report.",
            properties={
                "group_id": {
                    "type": "string",
                    "description": "QQ group id. Required in private chat; optional in a group chat.",
                },
                FEATURE_AI_CHAT: {"type": "boolean", "description": "AI 对话开关。"},
                FEATURE_COLLECTOR: {"type": "boolean", "description": "消息采集开关。"},
                FEATURE_DAILY_REPORT: {"type": "boolean", "description": "日报生成开关。"},
                FEATURE_DAILY_REPORT_AUTO: {"type": "boolean", "description": "自动发送日报开关。"},
                FEATURE_COMPANION: {"type": "boolean", "description": "陪伴画像开关。"},
                FEATURE_BOT_TEASE: {"type": "boolean", "description": "调戏其他 bot 开关。"},
                FEATURE_CONSTANT_RETORT: {"type": "boolean", "description": "常数回怼开关。"},
                FEATURE_KEYWORD_RETORT: {"type": "boolean", "description": "关键词回怼开关。"},
                "features": {
                    "type": "object",
                    "description": "Feature aliases to boolean values, for example {'日报': true}.",
                    "additionalProperties": {"type": "boolean"},
                },
                "limits": {
                    "type": "object",
                    "description": "Optional rate limits by feature, e.g. {'关键词回怼': {'per_minute': 5, 'per_hour': 20, 'per_day': 50}}.",
                    "additionalProperties": {
                        "type": "object",
                        "properties": {
                            "per_minute": {"type": "integer"},
                            "per_hour": {"type": "integer"},
                            "per_day": {"type": "integer"},
                        },
                        "additionalProperties": False,
                    },
                },
            },
        ),
        handler=set_group_features_tool,
    ),
]
