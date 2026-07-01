from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request as UrlRequest
from urllib.request import urlopen
from urllib.parse import unquote

import aiosqlite
from dotenv import load_dotenv
from nonebot import get_bot, get_driver

from plugins.access_control import (
    DB_PATH as ACCESS_DB_PATH,
    FEATURE_AI_CHAT,
    FEATURE_BOT_TEASE,
    FEATURE_COLLECTOR,
    FEATURE_COMPANION,
    FEATURE_CONSTANT_RETORT,
    FEATURE_DAILY_REPORT_AUTO,
    init_access_db,
    get_group_feature_limits,
    get_group_feature_usage,
    is_group_feature_enabled,
    set_group_feature,
    set_group_feature_limits,
)
from plugins.chime_service import (
    CHIME_MODE_HOURLY,
    get_chime_state as get_target_chime_state,
    init_chime_db,
    list_chime_group_ids,
    set_chime_state as set_target_chime_state,
)
from plugins.companion_memory import (
    DB_PATH as COMPANION_DB_PATH,
    bot_persona_prompt,
    delete_knowledge_item,
    get_group_profile,
    get_knowledge_item,
    get_profile,
    init_companion_memory_db,
    list_knowledge_items,
    now_text,
    save_bot_persona_prompt,
    save_group_profile,
    save_knowledge_item,
)
from plugins.companion_registry import init_companion_db, set_companion_target
from plugins.message_archive import DB_PATH as ARCHIVE_DB_PATH, init_archive_db
from sts_knowledge_seed import seed_sts_knowledge


load_dotenv(".env.local")

driver = get_driver()

ROUTE_PREFIX = "/hunterbot/admin-console"
ADMIN_TOKEN_ENV = "COMPANION_ADMIN_TOKEN"
STATIC_DIR = Path(__file__).resolve().parent / "static"
AVATAR_CACHE_DIR = Path("data/admin_console/group_avatars")
AVATAR_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
AVATAR_FETCH_TIMEOUT_SECONDS = 8
KIND_ORDER = (
    "card",
    "character",
    "relic",
    "potion",
    "enemy",
    "elite",
    "boss",
    "event",
    "mechanic",
    "keyword",
    "power",
    "enchantment",
    "guide",
)
TREE_ORDER = ("STS2",)
FEATURE_DAILY_REPORT = "daily_report"
FEATURE_CHIME = "hourly_chime"

try:
    from fastapi import Header, HTTPException, Query, Request
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
except Exception:  # pragma: no cover
    Header = None
    HTTPException = None
    Query = None
    Request = None
    FileResponse = None
    HTMLResponse = None
    JSONResponse = None


def admin_token() -> str:
    return os.getenv(ADMIN_TOKEN_ENV, "").strip()


def check_token(token: str | None = None, authorization: str | None = None) -> None:
    expected = admin_token()
    if not expected:
        raise HTTPException(status_code=403, detail=f"{ADMIN_TOKEN_ENV} is not configured")

    provided = (token or "").strip()
    if not provided and authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            provided = value.strip()
    if provided != expected:
        raise HTTPException(status_code=401, detail="invalid admin token")


def row_to_dict(row: aiosqlite.Row | None) -> dict[str, object] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def parse_topics(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
        return [item.strip() for item in text.replace("，", ",").replace("、", ",").split(",") if item.strip()]
    return []


def normalize_bool(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def normalize_limit_payload(value: object) -> dict[str, int]:
    source = value if isinstance(value, dict) else {}
    result: dict[str, int] = {}
    for key in ("per_minute", "per_hour", "per_day"):
        try:
            number = int(source.get(key, 0))
        except (TypeError, ValueError):
            number = 0
        result[key] = min(max(number, 0), 999)
    return result


def normalize_keywords_payload(value: object) -> list[str]:
    if isinstance(value, str):
        raw_items = value.replace("，", ",").replace("、", ",").split(",")
    elif isinstance(value, list):
        raw_items = [str(item) for item in value]
    else:
        raw_items = []
    keywords: list[str] = []
    for item in raw_items:
        keyword = item.strip()
        if keyword and keyword not in keywords:
            keywords.append(keyword)
    return keywords[:20]


def normalize_profile_payload(payload: dict[str, object]) -> dict[str, object]:
    try:
        confidence = float(payload.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "summary": str(payload.get("summary") or "").strip()[:1000],
        "current_activity": str(payload.get("current_activity") or "").strip()[:1000],
        "personality_notes": str(payload.get("personality_notes") or "").strip()[:1000],
        "emotional_preferences": str(payload.get("emotional_preferences") or "").strip()[:1000],
        "topics": parse_topics(payload.get("topics"))[:20],
        "confidence": min(max(confidence, 0.0), 1.0),
    }


async def ensure_console_databases() -> None:
    await init_access_db()
    await init_archive_db()
    await init_companion_db()
    await init_companion_memory_db()
    await init_chime_db()


def group_label(row: aiosqlite.Row | dict[str, object]) -> str:
    name = str(row["group_name"] if isinstance(row, aiosqlite.Row) else row.get("group_name") or "").strip()
    group_id = str(row["group_id"] if isinstance(row, aiosqlite.Row) else row.get("group_id") or "").strip()
    return name or f"群 {group_id}"


def parse_json_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    try:
        parsed = json.loads(str(value or "[]"))
    except Exception:
        parsed = None
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [part.strip() for part in str(value or "").replace("，", ",").replace("、", ",").split(",") if part.strip()]


def preview_text(value: object, limit: int = 120) -> str:
    text = " ".join(str(value or "").split()).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def normalize_group_id_for_avatar(group_id: str) -> str:
    safe_group_id = "".join(char for char in str(group_id) if char.isdigit())
    if not safe_group_id:
        raise HTTPException(status_code=400, detail="invalid group id")
    return safe_group_id


def group_avatar_cache_path(group_id: str) -> Path:
    safe_group_id = normalize_group_id_for_avatar(group_id)
    return AVATAR_CACHE_DIR / f"{safe_group_id}.jpg"


def group_avatar_url(group_id: str) -> str:
    group_id = normalize_group_id_for_avatar(group_id)
    return f"{ROUTE_PREFIX}/group-avatars/{group_id}.jpg"


def qq_group_avatar_source_url(group_id: str) -> str:
    group_id = normalize_group_id_for_avatar(group_id)
    return f"https://p.qlogo.cn/gh/{group_id}/{group_id}/100"


def avatar_cache_fresh(path: Path) -> bool:
    if not path.exists() or not path.is_file() or path.stat().st_size <= 0:
        return False
    return time.time() - path.stat().st_mtime < AVATAR_CACHE_TTL_SECONDS


def fetch_group_avatar_data(group_id: str) -> tuple[str, bytes]:
    request = UrlRequest(
        qq_group_avatar_source_url(group_id),
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urlopen(request, timeout=AVATAR_FETCH_TIMEOUT_SECONDS) as response:
        content_type = str(response.headers.get("Content-Type") or "")
        data = response.read(256 * 1024)
    return content_type, data


async def ensure_group_avatar(group_id: str) -> Path:
    path = group_avatar_cache_path(group_id)
    if avatar_cache_fresh(path):
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        content_type, data = await asyncio.to_thread(fetch_group_avatar_data, group_id)
    except (OSError, URLError) as exc:
        if path.exists() and path.is_file() and path.stat().st_size > 0:
            return path
        raise HTTPException(status_code=404, detail="group avatar unavailable") from exc

    if not data or "image" not in content_type.lower():
        raise HTTPException(status_code=404, detail="group avatar unavailable")
    path.write_bytes(data)
    return path


def group_name_from_payload(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("group_name") or payload.get("group_remark") or "").strip()


def user_avatar_url(user_id: str) -> str:
    safe_user_id = "".join(char for char in str(user_id) if char.isdigit())
    if not safe_user_id:
        return ""
    return f"https://q1.qlogo.cn/g?b=qq&nk={safe_user_id}&s=100"


def member_name_from_payload(payload: dict[str, object]) -> str:
    card = str(payload.get("card") or "").strip()
    nickname = str(payload.get("nickname") or "").strip()
    return card or nickname


def member_title_from_payload(payload: dict[str, object]) -> str:
    title = ""
    for key in ("special_title", "title", "honor"):
        value = str(payload.get(key) or "").strip()
        if value:
            title = value
            break
    level = ""
    for key in ("level", "group_level", "member_level", "level_name"):
        value = str(payload.get(key) or "").strip()
        if value:
            level = value
            break
    parts: list[str] = []
    if level:
        level_text = level if level.upper().startswith("LV") else f"LV{level}"
        parts.append(level_text)
    if title and title != level:
        parts.append(title)
    return " ".join(parts)


def normalize_member_payload(group_id: str, payload: dict[str, object]) -> dict[str, object] | None:
    user_id = str(payload.get("user_id") or "").strip()
    if not user_id:
        return None
    display_name = member_name_from_payload(payload) or user_id
    return {
        "group_id": group_id,
        "user_id": user_id,
        "display_name": display_name,
        "nickname": str(payload.get("nickname") or "").strip(),
        "card": str(payload.get("card") or "").strip(),
        "title": member_title_from_payload(payload),
        "role": str(payload.get("role") or "").strip(),
        "avatar_url": user_avatar_url(user_id),
    }


def group_rows_from_payload(payload: object) -> list[dict[str, object]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "groups", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


async def live_group_members(group_id: str) -> list[dict[str, object]]:
    try:
        bot = get_bot()
    except Exception:
        return []
    try:
        payload = await bot.call_api("get_group_member_list", group_id=int(group_id), no_cache=False)
    except Exception:
        return []
    members: list[dict[str, object]] = []
    for item in group_rows_from_payload(payload):
        member = normalize_member_payload(group_id, item)
        if member:
            members.append(member)
    members.sort(key=lambda item: (str(item.get("display_name") or "").lower(), str(item.get("user_id") or "")))
    return members


async def live_group_member_detail(group_id: str, user_id: str) -> dict[str, object] | None:
    try:
        bot = get_bot()
        payload = await bot.call_api(
            "get_group_member_info",
            group_id=int(group_id),
            user_id=int(user_id),
            no_cache=False,
        )
    except Exception:
        return None
    return normalize_member_payload(group_id, payload) if isinstance(payload, dict) else None


async def resolve_group_names(group_ids: list[str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    if not group_ids:
        return resolved
    try:
        bot = get_bot()
    except Exception:
        return resolved

    try:
        payload = await bot.call_api("get_group_list", no_cache=False)
    except Exception:
        payload = None
    for item in group_rows_from_payload(payload):
        group_id = str(item.get("group_id") or "").strip()
        group_name = group_name_from_payload(item)
        if group_id and group_name:
            resolved[group_id] = group_name

    missing_group_ids = [group_id for group_id in group_ids if group_id and group_id not in resolved]
    for group_id in missing_group_ids:
        try:
            payload = await bot.call_api("get_group_info", group_id=int(group_id), no_cache=False)
        except Exception:
            continue
        group_name = group_name_from_payload(payload)
        if group_name:
            resolved[group_id] = group_name
    return resolved


async def archived_groups() -> list[dict[str, object]]:
    if not ARCHIVE_DB_PATH.exists():
        return []
    async with aiosqlite.connect(ARCHIVE_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                group_id,
                COUNT(*) AS message_count,
                MAX(created_at) AS last_message_at
            FROM collected_messages
            GROUP BY group_id
            ORDER BY last_message_at DESC, group_id ASC
            """
        )
        rows = await cursor.fetchall()
    return [row_to_dict(row) or {} for row in rows]


async def configured_group_ids() -> list[str]:
    group_ids: set[str] = set()
    await init_access_db()
    await init_companion_db()
    async with aiosqlite.connect(ACCESS_DB_PATH) as db:
        cursor = await db.execute("SELECT DISTINCT group_id FROM group_feature_settings ORDER BY group_id ASC")
        group_ids.update(str(row[0]) for row in await cursor.fetchall() if row[0])
    if COMPANION_DB_PATH.exists():
        async with aiosqlite.connect(COMPANION_DB_PATH) as db:
            cursor = await db.execute("SELECT DISTINCT group_id FROM companion_targets ORDER BY group_id ASC")
            group_ids.update(str(row[0]) for row in await cursor.fetchall() if row[0])
    group_ids.update(await list_chime_group_ids())
    return sorted(group_ids)


async def list_groups() -> list[dict[str, object]]:
    archived = {str(item.get("group_id")): item for item in await archived_groups() if item.get("group_id")}
    for group_id in await configured_group_ids():
        archived.setdefault(group_id, {"group_id": group_id, "message_count": 0, "last_message_at": ""})
    group_ids = [str(item.get("group_id") or "").strip() for item in archived.values() if str(item.get("group_id") or "").strip()]
    live_names = await resolve_group_names(group_ids)
    groups = list(archived.values())
    groups.sort(key=lambda item: (str(item.get("last_message_at") or ""), str(item.get("group_id") or "")), reverse=True)
    result: list[dict[str, object]] = []
    for item in groups:
        group_id = str(item.get("group_id") or "").strip()
        if not group_id:
            continue
        group_name = live_names.get(group_id, str(item.get("group_name") or "")).strip()
        result.append(
            {
                "group_id": group_id,
                "group_name": group_name,
                "display_name": group_name or f"群 {group_id}",
                "avatar_url": group_avatar_url(group_id),
                "message_count": int(item.get("message_count") or 0),
                "last_message_at": str(item.get("last_message_at") or ""),
            }
        )
    return result


async def get_chime_state(group_id: str) -> dict[str, object]:
    state = await get_target_chime_state("group", group_id)
    return {
        "enabled": bool(state.get("enabled")),
        "mode": str(state.get("mode") or CHIME_MODE_HOURLY),
        "updated_at": str(state.get("updated_at") or ""),
    }


async def group_archive_state(
    group_id: str,
    limit: int = 12,
    offset: int = 0,
    user_id: str | None = None,
) -> dict[str, object]:
    enabled = await is_group_feature_enabled(group_id, FEATURE_COLLECTOR)
    filter_user_id = str(user_id or "").strip()
    safe_limit = max(1, min(int(limit or 12), 100))
    safe_offset = max(0, int(offset or 0))
    if not ARCHIVE_DB_PATH.exists():
        return {
            "enabled": enabled,
            "message_count": 0,
            "filtered_count": 0,
            "last_message_at": "",
            "recent_messages": [],
            "limit": safe_limit,
            "offset": safe_offset,
            "has_more": False,
            "filter_user_id": filter_user_id,
        }

    where_parts = ["group_id = ?"]
    where_params: list[object] = [group_id]
    if filter_user_id:
        where_parts.append("user_id = ?")
        where_params.append(filter_user_id)
    where_sql = " AND ".join(where_parts)

    async with aiosqlite.connect(ARCHIVE_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        summary_cursor = await db.execute(
            """
            SELECT COUNT(*) AS message_count, MAX(created_at) AS last_message_at
            FROM collected_messages
            WHERE group_id = ?
            """,
            (group_id,),
        )
        summary_row = await summary_cursor.fetchone()
        filtered_cursor = await db.execute(
            f"""
            SELECT COUNT(*) AS filtered_count
            FROM collected_messages
            WHERE {where_sql}
            """,
            where_params,
        )
        filtered_row = await filtered_cursor.fetchone()
        cursor = await db.execute(
            f"""
            SELECT id, user_id, sender_name, message_type, sub_type, segment_types, plain_text, created_at
            FROM collected_messages
            WHERE {where_sql}
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (*where_params, safe_limit, safe_offset),
        )
        rows = await cursor.fetchall()

    recent_messages: list[dict[str, object]] = []
    for row in rows:
        sender_name = str(row["sender_name"] or "").strip()
        user_id = str(row["user_id"] or "").strip()
        segment_types = parse_json_list(row["segment_types"])
        recent_messages.append(
            {
                "id": int(row["id"] or 0),
                "user_id": user_id,
                "sender_name": sender_name,
                "display_name": sender_name or user_id,
                "message_type": str(row["message_type"] or ""),
                "sub_type": str(row["sub_type"] or ""),
                "segment_types": segment_types,
                "plain_text": str(row["plain_text"] or ""),
                "preview_text": preview_text(row["plain_text"]),
                "created_at": str(row["created_at"] or ""),
            }
        )

    return {
        "enabled": enabled,
        "message_count": int(summary_row["message_count"] or 0) if summary_row else 0,
        "filtered_count": int(filtered_row["filtered_count"] or 0) if filtered_row else 0,
        "last_message_at": str(summary_row["last_message_at"] or "") if summary_row else "",
        "recent_messages": recent_messages,
        "limit": safe_limit,
        "offset": safe_offset,
        "has_more": safe_offset + len(recent_messages) < (int(filtered_row["filtered_count"] or 0) if filtered_row else 0),
        "filter_user_id": filter_user_id,
    }


async def set_chime_state(group_id: str, enabled: bool, mode: str = CHIME_MODE_HOURLY) -> None:
    await set_target_chime_state("group", group_id, enabled, mode)


async def get_daily_report_group_state(group_id: str) -> dict[str, object]:
    return {
        "enabled": await is_group_feature_enabled(group_id, FEATURE_COLLECTOR),
        "source_feature": FEATURE_COLLECTOR,
    }


async def set_daily_report_group_state(group_id: str, enabled: bool) -> None:
    await set_group_feature(group_id, FEATURE_COLLECTOR, enabled)


async def group_state(group_id: str) -> dict[str, object]:
    return {
        "group_id": group_id,
        "features": {
            FEATURE_AI_CHAT: await is_group_feature_enabled(group_id, FEATURE_AI_CHAT),
            FEATURE_DAILY_REPORT: (await get_daily_report_group_state(group_id))["enabled"],
            FEATURE_DAILY_REPORT_AUTO: await is_group_feature_enabled(group_id, FEATURE_DAILY_REPORT_AUTO),
            FEATURE_COMPANION: await is_group_feature_enabled(group_id, FEATURE_COMPANION),
            FEATURE_CHIME: (await get_chime_state(group_id))["enabled"],
            FEATURE_BOT_TEASE: await is_group_feature_enabled(group_id, FEATURE_BOT_TEASE),
            FEATURE_CONSTANT_RETORT: await is_group_feature_enabled(group_id, FEATURE_CONSTANT_RETORT),
        },
        "limits": {
            FEATURE_BOT_TEASE: await get_group_feature_limits(group_id, FEATURE_BOT_TEASE),
            FEATURE_CONSTANT_RETORT: await get_group_feature_limits(group_id, FEATURE_CONSTANT_RETORT),
        },
        "usage": {
            FEATURE_BOT_TEASE: await get_group_feature_usage(group_id, FEATURE_BOT_TEASE),
            FEATURE_CONSTANT_RETORT: await get_group_feature_usage(group_id, FEATURE_CONSTANT_RETORT),
        },
        "group_profile": await get_group_profile(group_id),
        "chime": await get_chime_state(group_id),
        "members": await list_group_members(group_id),
        "archive": await group_archive_state(group_id),
    }


async def save_group_state(group_id: str, payload: dict[str, object]) -> dict[str, object]:
    features = payload.get("features")
    if not isinstance(features, dict):
        features = {}
    if FEATURE_AI_CHAT in features:
        await set_group_feature(group_id, FEATURE_AI_CHAT, normalize_bool(features[FEATURE_AI_CHAT]))
    if FEATURE_DAILY_REPORT in features:
        daily_report_enabled = normalize_bool(features[FEATURE_DAILY_REPORT])
        await set_daily_report_group_state(group_id, daily_report_enabled)
        if not daily_report_enabled:
            await set_group_feature(group_id, FEATURE_DAILY_REPORT_AUTO, False)
    if FEATURE_DAILY_REPORT_AUTO in features:
        daily_report_enabled = (
            normalize_bool(features[FEATURE_DAILY_REPORT])
            if FEATURE_DAILY_REPORT in features
            else (await get_daily_report_group_state(group_id))["enabled"]
        )
        await set_group_feature(
            group_id,
            FEATURE_DAILY_REPORT_AUTO,
            normalize_bool(features[FEATURE_DAILY_REPORT_AUTO]) and bool(daily_report_enabled),
        )
    if FEATURE_COMPANION in features:
        companion_enabled = normalize_bool(features[FEATURE_COMPANION])
        await set_group_feature(group_id, FEATURE_COMPANION, companion_enabled)
        if companion_enabled:
            await set_group_feature(group_id, FEATURE_COLLECTOR, True)
    if FEATURE_BOT_TEASE in features:
        await set_group_feature(group_id, FEATURE_BOT_TEASE, normalize_bool(features[FEATURE_BOT_TEASE]))
    if FEATURE_CONSTANT_RETORT in features:
        await set_group_feature(group_id, FEATURE_CONSTANT_RETORT, normalize_bool(features[FEATURE_CONSTANT_RETORT]))
    limits = payload.get("limits") if isinstance(payload.get("limits"), dict) else {}
    bot_tease_limits = limits.get(FEATURE_BOT_TEASE) if isinstance(limits, dict) else None
    constant_retort_limits = limits.get(FEATURE_CONSTANT_RETORT) if isinstance(limits, dict) else None
    if isinstance(bot_tease_limits, dict):
        await set_group_feature_limits(group_id, FEATURE_BOT_TEASE, normalize_limit_payload(bot_tease_limits))
    if isinstance(constant_retort_limits, dict):
        await set_group_feature_limits(group_id, FEATURE_CONSTANT_RETORT, normalize_limit_payload(constant_retort_limits))
    group_profile = payload.get("group_profile") if isinstance(payload.get("group_profile"), dict) else {}
    if group_profile:
        await save_group_profile(
            group_id,
            group_profile.get("summary", ""),
            group_profile.get("max_chars", 100),
        )
    if FEATURE_CHIME in features:
        chime = payload.get("chime") if isinstance(payload.get("chime"), dict) else {}
        mode = str(chime.get("mode") or CHIME_MODE_HOURLY)
        await set_chime_state(group_id, normalize_bool(features[FEATURE_CHIME]), mode=mode)
    return await group_state(group_id)


async def target_rows_by_user_id(group_id: str) -> dict[str, dict[str, object]]:
    await init_companion_memory_db()
    async with aiosqlite.connect(COMPANION_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                t.group_id,
                t.user_id,
                t.display_name,
                t.enabled,
                t.is_bot,
                t.bot_keywords,
                t.selected_by,
                t.selected_at,
                t.updated_at,
                t.disabled_at,
                p.summary,
                p.updated_at AS profile_updated_at
            FROM companion_targets t
            LEFT JOIN companion_profiles p
              ON p.group_id = t.group_id AND p.user_id = t.user_id
            WHERE t.group_id = ?
            ORDER BY t.updated_at DESC
            """,
            (group_id,),
        )
        rows = await cursor.fetchall()
    return {str(row["user_id"]): row_to_dict(row) or {} for row in rows}


async def list_group_members(group_id: str) -> list[dict[str, object]]:
    live_members = await live_group_members(group_id)
    targets = await target_rows_by_user_id(group_id)
    seen_user_ids: set[str] = set()
    members: list[dict[str, object]] = []
    for member in live_members:
        user_id = str(member.get("user_id") or "")
        if not user_id:
            continue
        seen_user_ids.add(user_id)
        target = targets.get(user_id, {})
        member["target_enabled"] = bool(target.get("enabled"))
        member["selected_at"] = str(target.get("selected_at") or "")
        member["profile_updated_at"] = str(target.get("profile_updated_at") or "")
        member["summary"] = str(target.get("summary") or "")
        member["is_bot"] = bool(target.get("is_bot"))
        member["bot_keywords"] = parse_json_list(target.get("bot_keywords"))
        members.append(member)

    for user_id, target in targets.items():
        if user_id in seen_user_ids or not target.get("enabled"):
            continue
        display_name = str(target.get("display_name") or "").strip() or user_id
        members.append(
            {
                "group_id": group_id,
                "user_id": user_id,
                "display_name": display_name,
                "nickname": display_name,
                "card": "",
                "title": "",
                "role": "",
                "avatar_url": user_avatar_url(user_id),
                "target_enabled": True,
                "selected_at": str(target.get("selected_at") or ""),
                "profile_updated_at": str(target.get("profile_updated_at") or ""),
                "summary": str(target.get("summary") or ""),
                "is_bot": bool(target.get("is_bot")),
                "bot_keywords": parse_json_list(target.get("bot_keywords")),
            }
        )
    return members


async def companion_detail(group_id: str, user_id: str) -> dict[str, object]:
    await init_companion_memory_db()
    async with aiosqlite.connect(COMPANION_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT group_id, user_id, display_name, enabled, is_bot, bot_keywords, selected_by, selected_at, updated_at, disabled_at
            FROM companion_targets
            WHERE group_id = ? AND user_id = ?
            """,
            (group_id, user_id),
        )
        target = await cursor.fetchone()
    profile = await get_profile(group_id, user_id)
    live_member = await live_group_member_detail(group_id, user_id)
    return {"target": row_to_dict(target), "member": live_member, "profile": row_to_dict(profile)}


async def save_companion_profile(group_id: str, user_id: str, payload: dict[str, object]) -> dict[str, object]:
    profile = normalize_profile_payload(payload)
    timestamp = now_text()
    await init_companion_memory_db()
    async with aiosqlite.connect(COMPANION_DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO companion_profiles (
                group_id,
                user_id,
                summary,
                current_activity,
                personality_notes,
                emotional_preferences,
                topics,
                confidence,
                source_message_from_id,
                source_message_to_id,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)
            ON CONFLICT(group_id, user_id) DO UPDATE SET
                summary = excluded.summary,
                current_activity = excluded.current_activity,
                personality_notes = excluded.personality_notes,
                emotional_preferences = excluded.emotional_preferences,
                topics = excluded.topics,
                confidence = excluded.confidence,
                updated_at = excluded.updated_at
            """,
            (
                group_id,
                user_id,
                profile["summary"],
                profile["current_activity"],
                profile["personality_notes"],
                profile["emotional_preferences"],
                json.dumps(profile["topics"], ensure_ascii=False),
                profile["confidence"],
                timestamp,
            ),
        )
        await db.commit()
    return await companion_detail(group_id, user_id)


async def save_companion_target(group_id: str, user_id: str, payload: dict[str, object], selected_by: str = "") -> dict[str, object]:
    display_name = str(payload.get("display_name") or "").strip()
    if not display_name:
        member = await live_group_member_detail(group_id, user_id)
        display_name = str((member or {}).get("display_name") or "").strip()
    is_bot = normalize_bool(payload.get("is_bot")) if "is_bot" in payload else None
    bot_keywords = (
        json.dumps(normalize_keywords_payload(payload.get("bot_keywords")), ensure_ascii=False)
        if "bot_keywords" in payload
        else None
    )
    await set_companion_target(
        group_id=group_id,
        user_id=user_id,
        display_name=display_name or None,
        enabled=normalize_bool(payload.get("enabled")),
        selected_by=selected_by or None,
        is_bot=is_bot,
        bot_keywords=bot_keywords,
    )
    return await companion_detail(group_id, user_id)


async def save_companion_targets_bulk(group_id: str, payload: dict[str, object], selected_by: str = "") -> dict[str, object]:
    raw_user_ids = payload.get("user_ids") if isinstance(payload.get("user_ids"), list) else []
    user_ids: list[str] = []
    for raw_user_id in raw_user_ids:
        user_id = str(raw_user_id or "").strip()
        if user_id.isdigit() and user_id not in user_ids:
            user_ids.append(user_id)
    if not user_ids:
        raise HTTPException(status_code=400, detail="user_ids is required")

    enabled = normalize_bool(payload.get("enabled"))
    live_members = {str(item.get("user_id") or ""): item for item in await live_group_members(group_id)}
    existing_targets = await target_rows_by_user_id(group_id)
    for user_id in user_ids:
        member = live_members.get(user_id) or {}
        display_name = str(member.get("display_name") or member.get("nickname") or user_id).strip()
        target_exists = user_id in existing_targets
        await set_companion_target(
            group_id=group_id,
            user_id=user_id,
            display_name=display_name or None,
            enabled=enabled,
            selected_by=selected_by or None,
            is_bot=None if target_exists else False,
            bot_keywords=None if target_exists else "[]",
        )
    return {
        "updated": len(user_ids),
        "enabled": enabled,
        "group": await group_state(group_id),
    }


async def reset_companion_profile(group_id: str, user_id: str) -> dict[str, object]:
    await init_companion_memory_db()
    timestamp = now_text()
    async with aiosqlite.connect(COMPANION_DB_PATH) as db:
        await db.execute("DELETE FROM companion_profiles WHERE group_id = ? AND user_id = ?", (group_id, user_id))
        await db.execute(
            "UPDATE companion_memories SET active = 0, updated_at = ? WHERE group_id = ? AND user_id = ?",
            (timestamp, group_id, user_id),
        )
        await db.execute("DELETE FROM companion_update_state WHERE group_id = ? AND user_id = ?", (group_id, user_id))
        await db.commit()
    return await companion_detail(group_id, user_id)


async def delete_companion_profile(group_id: str, user_id: str) -> dict[str, object]:
    await reset_companion_profile(group_id, user_id)
    await set_companion_target(
        group_id=group_id,
        user_id=user_id,
        enabled=False,
        selected_by="admin-console",
    )
    return {"deleted": True, "group": await group_state(group_id)}


async def knowledge_tree() -> dict[str, dict[str, list[dict[str, object]]]]:
    items = [row_to_dict(row) for row in await list_knowledge_items()]
    grouped: dict[str, dict[str, list[dict[str, object]]]] = {}
    for item in items:
        if not item:
            continue
        category = str(item.get("category") or "").strip()
        tree, _, kind = category.partition("/")
        tree = tree or "未分类"
        kind = kind or "other"
        grouped.setdefault(tree, {}).setdefault(kind, []).append(item)
    for tree in grouped.values():
        for rows in tree.values():
            rows.sort(key=lambda item: str(item.get("title") or "").lower())
    return grouped


async def knowledge_state() -> dict[str, object]:
    return {
        "tree_order": TREE_ORDER,
        "kind_order": KIND_ORDER,
        "tree": await knowledge_tree(),
    }


async def save_knowledge(payload: dict[str, object], item_id: int | None = None) -> dict[str, object]:
    saved_id = await save_knowledge_item(
        item_id=item_id,
        title=str(payload.get("title") or ""),
        content=str(payload.get("content") or ""),
        keywords=normalize_keywords_payload(payload.get("keywords")),
        category=str(payload.get("category") or ""),
        enabled=normalize_bool(payload.get("enabled", True)),
    )
    return await knowledge_detail(saved_id)


async def knowledge_detail(item_id: int) -> dict[str, object]:
    row = await get_knowledge_item(item_id)
    if row is None:
        raise HTTPException(status_code=404, detail="knowledge item not found")
    return {"item": row_to_dict(row)}


async def console_state() -> dict[str, object]:
    await ensure_console_databases()
    groups = await list_groups()
    selected_group = str(groups[0]["group_id"]) if groups else ""
    return {
        "persona": await bot_persona_prompt(),
        "groups": groups,
        "selected_group": selected_group,
        "group": await group_state(selected_group) if selected_group else None,
        "knowledge": await knowledge_state(),
        "version": "v1.2.1",
        "route_prefix": ROUTE_PREFIX,
    }


def static_file(name: str) -> Path:
    path = (STATIC_DIR / name).resolve()
    if STATIC_DIR.resolve() not in path.parents and path != STATIC_DIR.resolve():
        raise HTTPException(status_code=400, detail="invalid static path")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="static file not found")
    return path


server_app = getattr(driver, "server_app", None)
if (
    server_app is not None
    and HTTPException is not None
    and HTMLResponse is not None
    and JSONResponse is not None
    and FileResponse is not None
):

    @driver.on_startup
    async def startup_admin_console() -> None:
        await ensure_console_databases()

    @server_app.get(ROUTE_PREFIX, response_class=HTMLResponse)
    async def admin_console_page(token: str | None = Query(default=None)) -> HTMLResponse:
        check_token(token=token)
        return HTMLResponse(static_file("index.html").read_text(encoding="utf-8"))

    @server_app.get(f"{ROUTE_PREFIX}/static/{{asset_name}}")
    async def admin_console_static(asset_name: str):
        asset = static_file(unquote(asset_name))
        media_type = "text/css" if asset.suffix == ".css" else "application/javascript"
        return FileResponse(asset, media_type=media_type)

    @server_app.get(f"{ROUTE_PREFIX}/group-avatars/{{group_id}}.jpg")
    async def admin_console_group_avatar(
        group_id: str,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ):
        check_token(token=token, authorization=authorization)
        avatar_path = await ensure_group_avatar(group_id)
        return FileResponse(avatar_path, media_type="image/jpeg")

    @server_app.get(f"{ROUTE_PREFIX}/api/state")
    async def admin_console_state(
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        return JSONResponse(await console_state())

    @server_app.put(f"{ROUTE_PREFIX}/api/persona")
    async def admin_console_save_persona(
        request: Request,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        payload = await request.json()
        await save_bot_persona_prompt(str(payload.get("persona") or ""))
        return JSONResponse({"persona": await bot_persona_prompt()})

    @server_app.get(f"{ROUTE_PREFIX}/api/knowledge")
    async def admin_console_knowledge(
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        return JSONResponse(await knowledge_state())

    @server_app.post(f"{ROUTE_PREFIX}/api/knowledge/seed-sts")
    async def admin_console_seed_sts(
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        result = seed_sts_knowledge()
        return JSONResponse({"result": result, "knowledge": await knowledge_state()})

    @server_app.get(f"{ROUTE_PREFIX}/api/knowledge/{{item_id}}")
    async def admin_console_knowledge_detail(
        item_id: int,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        return JSONResponse(await knowledge_detail(item_id))

    @server_app.post(f"{ROUTE_PREFIX}/api/knowledge")
    async def admin_console_create_knowledge(
        request: Request,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        return JSONResponse(await save_knowledge(await request.json()))

    @server_app.put(f"{ROUTE_PREFIX}/api/knowledge/{{item_id}}")
    async def admin_console_update_knowledge(
        item_id: int,
        request: Request,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        return JSONResponse(await save_knowledge(await request.json(), item_id=item_id))

    @server_app.delete(f"{ROUTE_PREFIX}/api/knowledge/{{item_id}}")
    async def admin_console_delete_knowledge(
        item_id: int,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        await delete_knowledge_item(item_id)
        return JSONResponse({"deleted": True, "knowledge": await knowledge_state()})

    @server_app.get(f"{ROUTE_PREFIX}/api/groups")
    async def admin_console_groups(
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        return JSONResponse({"groups": await list_groups()})

    @server_app.post(f"{ROUTE_PREFIX}/api/progress-message")
    async def admin_console_progress_message(
        request: Request,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        payload = await request.json()
        group_id = str(payload.get("group_id") or "").strip()
        message = str(payload.get("message") or "").strip()
        if not group_id.isdigit():
            raise HTTPException(status_code=400, detail="invalid group id")
        if not message:
            raise HTTPException(status_code=400, detail="message is required")
        bot = get_bot()
        await bot.call_api("send_group_msg", group_id=int(group_id), message=message[:1800])
        return JSONResponse({"sent": True})

    @server_app.get(f"{ROUTE_PREFIX}/api/groups/{{group_id}}")
    async def admin_console_group_detail(
        group_id: str,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        return JSONResponse(await group_state(group_id))

    @server_app.put(f"{ROUTE_PREFIX}/api/groups/{{group_id}}")
    async def admin_console_save_group(
        group_id: str,
        request: Request,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        return JSONResponse(await save_group_state(group_id, await request.json()))

    @server_app.get(f"{ROUTE_PREFIX}/api/groups/{{group_id}}/archive")
    async def admin_console_group_archive(
        group_id: str,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
        limit: int = Query(default=50),
        offset: int = Query(default=0),
        user_id: str | None = Query(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        return JSONResponse(
            await group_archive_state(
                group_id,
                limit=limit,
                offset=offset,
                user_id=user_id,
            )
        )

    @server_app.get(f"{ROUTE_PREFIX}/api/groups/{{group_id}}/companions/{{user_id}}")
    async def admin_console_companion_detail(
        group_id: str,
        user_id: str,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        return JSONResponse(await companion_detail(group_id, user_id))

    @server_app.put(f"{ROUTE_PREFIX}/api/groups/{{group_id}}/companions/{{user_id}}")
    async def admin_console_save_companion(
        group_id: str,
        user_id: str,
        request: Request,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        return JSONResponse(await save_companion_profile(group_id, user_id, await request.json()))

    @server_app.put(f"{ROUTE_PREFIX}/api/groups/{{group_id}}/companions/{{user_id}}/target")
    async def admin_console_save_companion_target(
        group_id: str,
        user_id: str,
        request: Request,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        return JSONResponse(
            await save_companion_target(
                group_id,
                user_id,
                await request.json(),
                selected_by="admin-console",
            )
        )

    @server_app.put(f"{ROUTE_PREFIX}/api/groups/{{group_id}}/companions/targets/bulk")
    async def admin_console_save_companion_targets_bulk(
        group_id: str,
        request: Request,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        return JSONResponse(
            await save_companion_targets_bulk(
                group_id,
                await request.json(),
                selected_by="admin-console",
            )
        )

    @server_app.post(f"{ROUTE_PREFIX}/api/groups/{{group_id}}/companions/{{user_id}}/reset")
    async def admin_console_reset_companion(
        group_id: str,
        user_id: str,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        return JSONResponse(await reset_companion_profile(group_id, user_id))

    @server_app.delete(f"{ROUTE_PREFIX}/api/groups/{{group_id}}/companions/{{user_id}}")
    async def admin_console_delete_companion(
        group_id: str,
        user_id: str,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        return JSONResponse(await delete_companion_profile(group_id, user_id))
