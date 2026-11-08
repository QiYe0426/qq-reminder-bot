"""触发档位配置（R0）。

约定：
- **默认关闭**：没有配置行的群一律不开概率/关键词触发；
- 配置存 `data/bot_settings.db` 的 `group_tier_config`（与控制台其它配置同库）；
- 概率插话受「冷却」与「每日上限」双重约束，避免刷屏和账单失控。
"""

from __future__ import annotations

import os
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

DB_PATH = Path(os.getenv("AGENT_TIER_DB_PATH", "data/bot_settings.db"))

DEFAULT_PROBABILITY = 5.0
DEFAULT_COOLDOWN_MINUTES = 10
DEFAULT_DAILY_LIMIT = 60

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS group_tier_config (
    group_id            TEXT PRIMARY KEY,
    enabled             INTEGER NOT NULL DEFAULT 0,
    keywords            TEXT NOT NULL DEFAULT '',
    probability         REAL NOT NULL DEFAULT 5,
    cooldown_minutes    INTEGER NOT NULL DEFAULT 10,
    daily_limit         INTEGER NOT NULL DEFAULT 60,
    updated_at          TEXT NOT NULL
)
"""


async def init_tier_db(path: Path | None = None) -> None:
    db_path = path or DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(_CREATE_SQL)
        await db.commit()


def _row_to_config(row: aiosqlite.Row | None, group_id: str) -> dict[str, Any]:
    if row is None:
        return {
            "group_id": str(group_id),
            "enabled": False,
            "keywords": "",
            "probability": DEFAULT_PROBABILITY,
            "cooldown_minutes": DEFAULT_COOLDOWN_MINUTES,
            "daily_limit": DEFAULT_DAILY_LIMIT,
            "updated_at": "",
        }
    return {
        "group_id": str(row["group_id"]),
        "enabled": bool(row["enabled"]),
        "keywords": str(row["keywords"] or ""),
        "probability": float(row["probability"] or 0),
        "cooldown_minutes": int(row["cooldown_minutes"] or 0),
        "daily_limit": int(row["daily_limit"] or 0),
        "updated_at": str(row["updated_at"] or ""),
    }


async def get_tier_config(group_id: str | int, *, path: Path | None = None) -> dict[str, Any]:
    db_path = path or DB_PATH
    await init_tier_db(db_path)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM group_tier_config WHERE group_id = ?", (str(group_id),)
        )
        row = await cursor.fetchone()
    return _row_to_config(row, str(group_id))


def normalize_config(group_id: str | int, payload: dict[str, Any]) -> dict[str, Any]:
    keywords = payload.get("keywords", "")
    if isinstance(keywords, list):
        keywords = ",".join(str(item).strip() for item in keywords)
    keywords = ",".join(
        part.strip() for part in str(keywords or "").replace("，", ",").split(",") if part.strip()
    )[:400]
    try:
        probability = float(payload.get("probability", DEFAULT_PROBABILITY))
    except (TypeError, ValueError):
        probability = DEFAULT_PROBABILITY
    probability = max(0.0, min(probability, 100.0))
    try:
        cooldown = int(payload.get("cooldown_minutes", DEFAULT_COOLDOWN_MINUTES))
    except (TypeError, ValueError):
        cooldown = DEFAULT_COOLDOWN_MINUTES
    cooldown = max(0, min(cooldown, 24 * 60))
    try:
        daily_limit = int(payload.get("daily_limit", DEFAULT_DAILY_LIMIT))
    except (TypeError, ValueError):
        daily_limit = DEFAULT_DAILY_LIMIT
    daily_limit = max(0, min(daily_limit, 10000))
    return {
        "group_id": str(group_id),
        "enabled": bool(payload.get("enabled")),
        "keywords": keywords,
        "probability": probability,
        "cooldown_minutes": cooldown,
        "daily_limit": daily_limit,
    }


async def save_tier_config(
    group_id: str | int,
    payload: dict[str, Any],
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    config = normalize_config(group_id, payload)
    db_path = path or DB_PATH
    await init_tier_db(db_path)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO group_tier_config
                (group_id, enabled, keywords, probability, cooldown_minutes, daily_limit, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                enabled = excluded.enabled,
                keywords = excluded.keywords,
                probability = excluded.probability,
                cooldown_minutes = excluded.cooldown_minutes,
                daily_limit = excluded.daily_limit,
                updated_at = excluded.updated_at
            """,
            (
                config["group_id"],
                1 if config["enabled"] else 0,
                config["keywords"],
                config["probability"],
                config["cooldown_minutes"],
                config["daily_limit"],
                timestamp,
            ),
        )
        await db.commit()
    config["updated_at"] = timestamp
    return config


async def decide_trigger(
    *,
    group_id: str | int,
    text: str,
    mentioned: bool,
    run_count_today: int,
    last_run_at: int | None,
    now: float | None = None,
    path: Path | None = None,
    rng: Any = None,
) -> tuple[bool, str]:
    """R0 决策：是否允许把这条消息送进对话。

    返回 (是否触发, 原因)。``mentioned`` 为 @ / 叫名字，永远触发。
    """
    if mentioned:
        return True, "mention"
    config = await get_tier_config(group_id, path=path)
    if not config["enabled"]:
        return False, "tier_disabled"

    keywords = [item.strip() for item in str(config["keywords"]).split(",") if item.strip()]
    lowered = (text or "").lower()
    if keywords and any(keyword.lower() in lowered for keyword in keywords):
        return True, "keyword"

    probability = float(config["probability"] or 0)
    if probability <= 0:
        return False, "probability_zero"
    if int(config["daily_limit"]) and run_count_today >= int(config["daily_limit"]):
        return False, "daily_limit"
    cooldown_seconds = int(config["cooldown_minutes"]) * 60
    current = time.time() if now is None else now
    if cooldown_seconds and last_run_at and current - int(last_run_at) < cooldown_seconds:
        return False, "cooldown"

    roll = (rng or random.random)() * 100
    if roll < probability:
        return True, "probability"
    return False, "not_rolled"
