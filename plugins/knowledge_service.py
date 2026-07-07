from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

import aiosqlite


DB_PATH = Path("data/companion_memory.db")
DEFAULT_KNOWLEDGE_LOOKUP_LIMIT = 3
DEFAULT_KNOWLEDGE_MIN_SCORE = 2
DEFAULT_KNOWLEDGE_SCAN_LIMIT = 4000
MAX_KNOWLEDGE_CONTEXT_CHARS = 2200
MAX_TOOL_ITEM_CONTENT_CHARS = 1200

_knowledge_db_ready = False


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_int_env(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    raw_value = os.getenv(name)
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        return default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def shorten_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n..."


async def init_knowledge_db() -> None:
    global _knowledge_db_ready
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS companion_knowledge_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                keywords TEXT NOT NULL DEFAULT '[]',
                category TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_companion_knowledge_enabled_updated
            ON companion_knowledge_items (enabled, updated_at)
            """
        )
        await db.commit()
    _knowledge_db_ready = True


async def ensure_knowledge_db() -> None:
    if not _knowledge_db_ready:
        await init_knowledge_db()


def normalize_knowledge_category(category: str) -> str:
    raw = " ".join((category or "").split()).strip()
    if not raw:
        return ""

    raw = raw.replace("／", "/").replace("\\", "/").replace("-", "/").replace("_", "/")
    parts = [part.strip() for part in raw.split("/") if part.strip()]
    if not parts:
        return ""

    def normalize_tree(value: str) -> str:
        compact = re.sub(r"[\s.]+", "", value.lower())
        mapping = {
            "sts2": "STS2",
            "sts-2": "STS2",
            "sts二": "STS2",
            "slaythespire2": "STS2",
            "slaythespireii": "STS2",
            "杀戮尖塔2": "STS2",
            "塔2": "STS2",
            "二代": "STS2",
            "2代": "STS2",
            "二": "STS2",
        }
        return mapping.get(compact, value.strip().upper()[:16])

    def normalize_kind(value: str) -> str:
        compact = re.sub(r"[\s.]+", "", value.lower())
        mapping = {
            "card": "card",
            "卡牌": "card",
            "牌": "card",
            "character": "character",
            "characters": "character",
            "角色": "character",
            "职业": "character",
            "relic": "relic",
            "relics": "relic",
            "遗物": "relic",
            "potion": "potion",
            "potions": "potion",
            "药水": "potion",
            "enemy": "enemy",
            "monster": "enemy",
            "monsters": "enemy",
            "怪物": "enemy",
            "敌人": "enemy",
            "小怪": "enemy",
            "elite": "elite",
            "精英": "elite",
            "boss": "boss",
            "首领": "boss",
            "event": "event",
            "events": "event",
            "事件": "event",
            "mechanic": "mechanic",
            "mechanics": "mechanic",
            "机制": "mechanic",
            "keyword": "keyword",
            "keywords": "keyword",
            "关键词": "keyword",
            "术语": "mechanic",
            "power": "power",
            "powers": "power",
            "能力": "power",
            "状态": "power",
            "enchantment": "enchantment",
            "enchantments": "enchantment",
            "附魔": "enchantment",
            "guide": "guide",
            "攻略": "guide",
            "指南": "guide",
            "词典": "mechanic",
        }
        return mapping.get(compact, value.strip().lower()[:16])

    tree = normalize_tree(parts[0])
    if len(parts) == 1:
        return tree
    kind = normalize_kind(parts[1])
    return f"{tree}/{kind}" if kind else tree


def tokenize_for_lookup(text: str) -> list[str]:
    normalized = text.lower()
    raw_tokens = re.findall(r"[a-z0-9_+\-.#]{2,}|[\u4e00-\u9fff]{2,}", normalized)
    tokens: list[str] = []
    for token in raw_tokens:
        token = token.strip()
        if token and token not in tokens:
            tokens.append(token)
    return tokens[:80]


def parse_keywords(raw_keywords: object) -> list[str]:
    try:
        loaded_keywords = json.loads(str(raw_keywords or "[]"))
    except json.JSONDecodeError:
        return []
    if not isinstance(loaded_keywords, list):
        return []
    return [str(item).lower() for item in loaded_keywords]


def knowledge_score(row: aiosqlite.Row, question_tokens: list[str], *, query_text: str = "") -> int:
    title = str(row["title"] or "").lower()
    content = str(row["content"] or "").lower()
    category = normalize_knowledge_category(str(row["category"] or "")).lower()
    keywords = parse_keywords(row["keywords"])

    score = 0
    if query_text:
        if title and (title in query_text or query_text in title):
            score += 8
        if category and category in query_text:
            score += 3
        for keyword in keywords:
            if keyword and keyword in query_text:
                score += 6

    for token in question_tokens:
        if token in keywords:
            score += 5
        if token in title:
            score += 4
        if token in category:
            score += 3
        if token in content:
            score += 1
    return score


def row_to_knowledge_item(row: aiosqlite.Row, *, score: int) -> dict[str, object]:
    return {
        "id": int(row["id"] or 0),
        "title": str(row["title"] or ""),
        "category": str(row["category"] or ""),
        "content": str(row["content"] or ""),
        "keywords": parse_keywords(row["keywords"]),
        "updated_at": str(row["updated_at"] or ""),
        "score": score,
    }


async def search_knowledge_items(
    query: str,
    *,
    category_prefix: str | None = None,
    limit: int | None = None,
    min_score: int | None = None,
    scan_limit: int | None = None,
) -> list[dict[str, object]]:
    question_tokens = tokenize_for_lookup(query)
    if not question_tokens:
        return []

    result_limit = max(1, min(limit or DEFAULT_KNOWLEDGE_LOOKUP_LIMIT, 10))
    score_floor = min_score if min_score is not None else get_int_env(
        "COMPANION_KNOWLEDGE_MIN_SCORE",
        DEFAULT_KNOWLEDGE_MIN_SCORE,
        minimum=1,
        maximum=100,
    )
    read_limit = scan_limit if scan_limit is not None else get_int_env(
        "COMPANION_KNOWLEDGE_SCAN_LIMIT",
        DEFAULT_KNOWLEDGE_SCAN_LIMIT,
        minimum=1,
        maximum=10000,
    )

    normalized_prefix = normalize_knowledge_category(category_prefix or "")
    where = "WHERE enabled = 1"
    params: list[object] = []
    if normalized_prefix:
        where += " AND (category = ? OR category LIKE ?)"
        params.extend([normalized_prefix, f"{normalized_prefix}/%"])
    params.append(read_limit)

    await ensure_knowledge_db()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT id, title, content, keywords, category, updated_at
            FROM companion_knowledge_items
            {where}
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            tuple(params),
        )
        rows = await cursor.fetchall()

    normalized_query = query.lower()
    scored_items = [
        (knowledge_score(row, question_tokens, query_text=normalized_query), row)
        for row in rows
    ]
    matched_items = [
        row_to_knowledge_item(row, score=score)
        for score, row in sorted(scored_items, key=lambda item: item[0], reverse=True)
        if score >= score_floor
    ]
    return matched_items[:result_limit]


def render_knowledge_context(items: list[dict[str, object]], *, max_chars: int = MAX_KNOWLEDGE_CONTEXT_CHARS) -> str:
    if not items:
        return ""

    sections: list[str] = []
    for item in items:
        title = str(item.get("title") or "").strip()
        category = str(item.get("category") or "").strip()
        content = str(item.get("content") or "").strip()
        header = f"- {title}"
        if category:
            header += f"（{category}）"
        sections.append(f"{header}\n{content}")

    context = "\n\n".join(sections)
    return shorten_text(context, max_chars)


async def knowledge_reply_context(question: str) -> str:
    limit = get_int_env(
        "COMPANION_KNOWLEDGE_LOOKUP_LIMIT",
        DEFAULT_KNOWLEDGE_LOOKUP_LIMIT,
        minimum=0,
        maximum=10,
    )
    if limit <= 0:
        return ""

    items = await search_knowledge_items(question, limit=limit)
    context = render_knowledge_context(items)
    if not context:
        return ""

    return (
        "以下是本地知识库中与当前问题相关的内容。"
        "只有当内容确实相关时才使用；如果知识库和用户问题不匹配，就忽略它。"
        "不要编造知识库没有提供的细节。\n\n"
        f"{context}"
    )


async def search_knowledge_result(
    query: str,
    *,
    category_prefix: str | None = None,
    limit: int = 5,
) -> dict[str, object]:
    safe_limit = max(1, min(int(limit or 5), 8))
    items = await search_knowledge_items(
        query,
        category_prefix=category_prefix,
        limit=safe_limit,
    )
    tool_items: list[dict[str, object]] = []
    for item in items:
        tool_items.append(
            {
                **item,
                "content": shorten_text(str(item.get("content") or ""), MAX_TOOL_ITEM_CONTENT_CHARS),
            }
        )
    return {
        "ok": True,
        "query": query,
        "category_prefix": normalize_knowledge_category(category_prefix or ""),
        "count": len(tool_items),
        "items": tool_items,
        "message": render_knowledge_context(tool_items, max_chars=MAX_TOOL_ITEM_CONTENT_CHARS * max(1, len(tool_items))),
    }
