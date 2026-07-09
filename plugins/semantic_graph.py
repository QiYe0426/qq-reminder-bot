from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from plugins.message_archive import DB_PATH as ARCHIVE_DB_PATH, escape_like_pattern, init_archive_db


DB_PATH = Path("data/semantic_graph.db")
DEFAULT_GRAPH_LIMIT = 240
MAX_GRAPH_LIMIT = 800
MAX_GRAPH_NODES = 36
MAX_GRAPH_EDGES = 80
REPORT_DAY_START_HOUR = 4


STOP_TERMS = {
    "一个",
    "一下",
    "不是",
    "不能",
    "不要",
    "东西",
    "为了",
    "什么",
    "他们",
    "但是",
    "你们",
    "其实",
    "刚刚",
    "可以",
    "因为",
    "如果",
    "已经",
    "应该",
    "我们",
    "所以",
    "时候",
    "昨天",
    "是不是",
    "有没有",
    "现在",
    "直接",
    "真的",
    "觉得",
    "这个",
    "这里",
    "这些",
    "这么",
    "还是",
    "那个",
    "那些",
    "那么",
    "然后",
    "比较",
    "没有",
    "就是",
    "the",
    "and",
    "for",
    "with",
    "this",
    "that",
    "from",
    "have",
    "you",
}
CHINESE_SPLITTERS = (
    "今天",
    "昨天",
    "现在",
    "然后",
    "但是",
    "就是",
    "可以",
    "觉得",
    "已经",
    "如果",
    "因为",
    "所以",
    "应该",
    "不是",
    "没有",
    "需要",
    "时候",
    "直接",
)
MEDIA_NODE_LABELS = {
    "image": "图片素材",
    "mface": "表情包",
    "record": "语音消息",
    "video": "视频素材",
    "file": "文件素材",
    "share": "分享链接",
    "forward": "聊天记录",
}


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clamp_limit(value: object, *, default: int = DEFAULT_GRAPH_LIMIT) -> int:
    try:
        limit = int(value or default)
    except (TypeError, ValueError):
        limit = default
    return max(1, min(limit, MAX_GRAPH_LIMIT))


def day_range(target_date: date) -> tuple[str, str]:
    start_at = datetime.combine(target_date, datetime.min.time()).replace(hour=REPORT_DAY_START_HOUR)
    end_at = start_at + timedelta(days=1)
    return start_at.strftime("%Y-%m-%d %H:%M:%S"), end_at.strftime("%Y-%m-%d %H:%M:%S")


def normalize_text(value: Any) -> str:
    text = re.sub(r"\[CQ:[^\]]+\]", " ", str(value or ""))
    text = re.sub(r"https?://\S+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_term(value: str) -> str:
    term = normalize_text(value).strip(" \t\r\n，。！？；：、,.!?;:()（）[]【】<>《》\"'`“”‘’")
    if re.fullmatch(r"[A-Za-z0-9_.+\-]{2,40}", term):
        term = term.lower()
    return term


def valid_term(term: str) -> bool:
    if len(term) < 2 or len(term) > 18:
        return False
    folded = term.casefold()
    if folded in STOP_TERMS or term in STOP_TERMS:
        return False
    if term.isdigit():
        return False
    if re.fullmatch(r"\d+[:：]\d+", term):
        return False
    return True


# Emoji 在 PIL 渲染中会变成方块，在话题关键词里无意义，故在提取时过滤掉。
# 群友昵称中的 emoji 不受影响（speaker_label 不做过滤）。
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FFFF"   # Supplementary Multilingual Plane — 绝大多数现代 emoji
    "\U00002600-\U000027BF"   # Miscellaneous Symbols + Dingbats
    "\U0000FE00-\U0000FE0F"   # Variation Selectors (肤色、性别等)
    "\U0000200D\U000020E3"    # ZWJ (组合用) + 结合围 keycap
    "\U0000231A-\U0000231B\U000023CF"  # 手表、沙漏、退出
    "\U000023E9-\U000023FA"   # 时间按钮 + 媒体控制
    "\U000024C2"              # 圆圈 M
    "\U000025AA-\U000025AB"   # 小黑白方
    "\U000025B6\U000025C0"    # 播放、倒退
    "\U000025FB-\U000025FE"   # 中方块
    "\U00002B05-\U00002B07"   # 箭头
    "\U00002B1B-\U00002B1C"   # 大方块
    "\U00002B50\U00002B55"    # 星星、圈
    "\U00003030\U0000303D"    # 波浪横、部分交替
    "\U00003297\U00003299"    # 圈中「合」「秘」
    "]+",
    re.UNICODE,
)


def strip_emoji(text: str) -> str:
    """移除文本中的 emoji 字符，用于话题关键词提取。"""
    return _EMOJI_RE.sub("", text)


def split_chinese_phrase(phrase: str) -> list[str]:
    parts = [phrase]
    for splitter in CHINESE_SPLITTERS:
        next_parts: list[str] = []
        for part in parts:
            next_parts.extend(item for item in part.split(splitter) if item)
        parts = next_parts

    terms: list[str] = []
    for part in parts:
        cleaned = normalize_term(part)
        if not cleaned:
            continue
        if len(cleaned) <= 10:
            terms.append(cleaned)
        else:
            terms.append(cleaned[:10])
    return terms


def parse_segment_types(raw: object) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if not raw:
        return []
    try:
        parsed = json.loads(str(raw))
    except Exception:
        return [str(raw)]
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return [str(parsed)]


def extract_terms_from_message(message: dict[str, str], *, max_terms: int = 10) -> list[str]:
    text = strip_emoji(normalize_text(message.get("plain_text")))
    terms: list[str] = []

    for raw in re.findall(r"[A-Za-z][A-Za-z0-9_.+\-]{1,32}", text):
        term = normalize_term(raw)
        if valid_term(term) and term not in terms:
            terms.append(term)

    for raw in re.findall(r"[\u4e00-\u9fff]{2,24}", text):
        for term in split_chinese_phrase(raw):
            if valid_term(term) and term not in terms:
                terms.append(term)
            if len(terms) >= max_terms:
                break
        if len(terms) >= max_terms:
            break

    for segment_type in parse_segment_types(message.get("segment_types")):
        label = MEDIA_NODE_LABELS.get(segment_type)
        if label and label not in terms:
            terms.append(label)

    return terms[:max_terms]


def speaker_label(message: dict[str, str]) -> str:
    name = normalize_text(message.get("sender_name"))
    if name:
        return name[:24]
    user_id = normalize_text(message.get("user_id"))
    return user_id or "未知群友"


def graph_id_for(
    *,
    group_id: str,
    scope: str,
    source_key: str,
    keyword: str,
    limit: int,
) -> str:
    digest = hashlib.sha1(f"{group_id}|{scope}|{source_key}|{keyword}|{limit}".encode("utf-8")).hexdigest()[:12]
    return f"{group_id}:{scope}:{digest}"


async def init_semantic_graph_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS semantic_graphs (
                graph_id TEXT PRIMARY KEY,
                group_id TEXT NOT NULL,
                scope TEXT NOT NULL,
                source_key TEXT NOT NULL,
                title TEXT NOT NULL,
                keyword TEXT NOT NULL DEFAULT '',
                message_count INTEGER NOT NULL DEFAULT 0,
                node_count INTEGER NOT NULL DEFAULT 0,
                edge_count INTEGER NOT NULL DEFAULT 0,
                params_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_semantic_graphs_group_updated
            ON semantic_graphs (group_id, updated_at)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS semantic_graph_nodes (
                graph_id TEXT NOT NULL,
                label TEXT NOT NULL,
                kind TEXT NOT NULL,
                weight REAL NOT NULL DEFAULT 1,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (graph_id, label, kind)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS semantic_graph_edges (
                graph_id TEXT NOT NULL,
                source_label TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                target_label TEXT NOT NULL,
                target_kind TEXT NOT NULL,
                relation TEXT NOT NULL,
                weight REAL NOT NULL DEFAULT 1,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (
                    graph_id,
                    source_label,
                    source_kind,
                    target_label,
                    target_kind,
                    relation
                )
            )
            """
        )
        await db.commit()


async def fetch_messages_for_graph(
    *,
    group_id: str,
    target_date: date | None = None,
    keyword: str = "",
    limit: int = DEFAULT_GRAPH_LIMIT,
) -> list[dict[str, str]]:
    await init_archive_db()
    safe_limit = clamp_limit(limit)
    cleaned_keyword = normalize_text(keyword)
    where = ["group_id = ?"]
    params: list[object] = [str(group_id)]

    if target_date is not None:
        start_at, end_at = day_range(target_date)
        where.append("created_at >= ?")
        where.append("created_at < ?")
        params.extend([start_at, end_at])
        order_sql = "created_at ASC, id ASC"
    else:
        order_sql = "created_at DESC, id DESC"

    if cleaned_keyword:
        where.append("COALESCE(plain_text, '') LIKE ? ESCAPE '\\'")
        params.append(f"%{escape_like_pattern(cleaned_keyword)}%")

    params.append(safe_limit)
    async with aiosqlite.connect(ARCHIVE_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT message_id, user_id, sender_name, plain_text, segment_types, created_at
            FROM collected_messages
            WHERE {' AND '.join(where)}
            ORDER BY {order_sql}
            LIMIT ?
            """,
            tuple(params),
        )
        rows = await cursor.fetchall()

    messages = [
        {
            "message_id": str(row["message_id"] or ""),
            "user_id": str(row["user_id"] or ""),
            "sender_name": str(row["sender_name"] or row["user_id"] or ""),
            "plain_text": str(row["plain_text"] or ""),
            "segment_types": str(row["segment_types"] or ""),
            "created_at": str(row["created_at"] or ""),
        }
        for row in rows
    ]
    if target_date is None:
        messages.reverse()
    return messages


def build_graph_from_messages(
    *,
    group_id: str,
    messages: list[dict[str, str]],
    scope: str,
    source_key: str,
    keyword: str = "",
    limit: int = DEFAULT_GRAPH_LIMIT,
) -> dict[str, object]:
    graph_id = graph_id_for(
        group_id=group_id,
        scope=scope,
        source_key=source_key,
        keyword=keyword,
        limit=limit,
    )
    title = f"群 {group_id} 语义图"
    if scope == "date":
        title += f"（{source_key}）"
    elif keyword:
        title += f"（关键词：{keyword}）"
    else:
        title += f"（最近 {len(messages)} 条）"

    speaker_counts: Counter[str] = Counter()
    term_counts: Counter[str] = Counter()
    message_terms: list[tuple[str, list[str]]] = []
    for message in messages:
        speaker = speaker_label(message)
        speaker_counts[speaker] += 1
        terms = extract_terms_from_message(message)
        term_counts.update(terms)
        message_terms.append((speaker, terms))

    top_speakers = [label for label, _count in speaker_counts.most_common(12)]
    min_term_count = 2 if len(messages) >= 12 else 1
    top_terms = [
        label
        for label, count in term_counts.most_common(MAX_GRAPH_NODES)
        if count >= min_term_count
    ][: max(8, MAX_GRAPH_NODES - len(top_speakers))]

    nodes: list[dict[str, object]] = []
    node_keys: set[tuple[str, str]] = set()
    for label in top_speakers:
        nodes.append(
            {
                "label": label,
                "kind": "person",
                "weight": float(speaker_counts[label]),
                "metadata": {"message_count": speaker_counts[label]},
            }
        )
        node_keys.add((label, "person"))
    for label in top_terms:
        nodes.append(
            {
                "label": label,
                "kind": "topic",
                "weight": float(term_counts[label]),
                "metadata": {"mention_count": term_counts[label]},
            }
        )
        node_keys.add((label, "topic"))

    speaker_topic_edges: Counter[tuple[str, str]] = Counter()
    topic_edges: Counter[tuple[str, str]] = Counter()
    speaker_edges: Counter[tuple[str, str]] = Counter()
    previous_speaker = ""
    for speaker, terms in message_terms:
        filtered_terms = [term for term in terms if term in top_terms]
        if speaker in top_speakers:
            for term in filtered_terms[:5]:
                speaker_topic_edges[(speaker, term)] += 1
        for index, left in enumerate(filtered_terms[:5]):
            for right in filtered_terms[index + 1 : 5]:
                if left == right:
                    continue
                pair = tuple(sorted((left, right)))
                topic_edges[pair] += 1
        if previous_speaker and speaker and previous_speaker != speaker:
            pair = tuple(sorted((previous_speaker, speaker)))
            if pair[0] in top_speakers and pair[1] in top_speakers:
                speaker_edges[pair] += 1
        previous_speaker = speaker

    edges: list[dict[str, object]] = []
    for (speaker, term), weight in speaker_topic_edges.most_common(MAX_GRAPH_EDGES):
        if (speaker, "person") in node_keys and (term, "topic") in node_keys:
            edges.append(
                {
                    "source": speaker,
                    "source_kind": "person",
                    "target": term,
                    "target_kind": "topic",
                    "relation": "提到",
                    "weight": float(weight),
                    "metadata": {},
                }
            )
    for (left, right), weight in topic_edges.most_common(MAX_GRAPH_EDGES):
        if (left, "topic") in node_keys and (right, "topic") in node_keys:
            edges.append(
                {
                    "source": left,
                    "source_kind": "topic",
                    "target": right,
                    "target_kind": "topic",
                    "relation": "相关",
                    "weight": float(weight),
                    "metadata": {},
                }
            )
    for (left, right), weight in speaker_edges.most_common(MAX_GRAPH_EDGES):
        if (left, "person") in node_keys and (right, "person") in node_keys:
            edges.append(
                {
                    "source": left,
                    "source_kind": "person",
                    "target": right,
                    "target_kind": "person",
                    "relation": "接续讨论",
                    "weight": float(math.sqrt(weight)),
                    "metadata": {"turn_count": weight},
                }
            )
    edges = sorted(edges, key=lambda item: float(item["weight"]), reverse=True)[:MAX_GRAPH_EDGES]

    return {
        "ok": True,
        "graph_id": graph_id,
        "group_id": str(group_id),
        "scope": scope,
        "source_key": source_key,
        "title": title,
        "keyword": keyword,
        "message_count": len(messages),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": nodes,
        "edges": edges,
        "generated_at": now_text(),
    }


async def save_semantic_graph(graph: dict[str, object], *, params: dict[str, object] | None = None) -> None:
    await init_semantic_graph_db()
    graph_id = str(graph["graph_id"])
    timestamp = now_text()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM semantic_graph_nodes WHERE graph_id = ?", (graph_id,))
        await db.execute("DELETE FROM semantic_graph_edges WHERE graph_id = ?", (graph_id,))
        await db.execute(
            """
            INSERT INTO semantic_graphs (
                graph_id, group_id, scope, source_key, title, keyword,
                message_count, node_count, edge_count, params_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(graph_id) DO UPDATE SET
                title = excluded.title,
                keyword = excluded.keyword,
                message_count = excluded.message_count,
                node_count = excluded.node_count,
                edge_count = excluded.edge_count,
                params_json = excluded.params_json,
                updated_at = excluded.updated_at
            """,
            (
                graph_id,
                str(graph["group_id"]),
                str(graph["scope"]),
                str(graph["source_key"]),
                str(graph["title"]),
                str(graph.get("keyword") or ""),
                int(graph.get("message_count") or 0),
                int(graph.get("node_count") or 0),
                int(graph.get("edge_count") or 0),
                json.dumps(params or {}, ensure_ascii=False, default=str),
                timestamp,
                timestamp,
            ),
        )
        for node in graph.get("nodes", []):
            if not isinstance(node, dict):
                continue
            await db.execute(
                """
                INSERT INTO semantic_graph_nodes (graph_id, label, kind, weight, metadata_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    graph_id,
                    str(node.get("label") or ""),
                    str(node.get("kind") or "topic"),
                    float(node.get("weight") or 1),
                    json.dumps(node.get("metadata") or {}, ensure_ascii=False, default=str),
                ),
            )
        for edge in graph.get("edges", []):
            if not isinstance(edge, dict):
                continue
            await db.execute(
                """
                INSERT INTO semantic_graph_edges (
                    graph_id, source_label, source_kind, target_label, target_kind,
                    relation, weight, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    graph_id,
                    str(edge.get("source") or ""),
                    str(edge.get("source_kind") or "topic"),
                    str(edge.get("target") or ""),
                    str(edge.get("target_kind") or "topic"),
                    str(edge.get("relation") or "相关"),
                    float(edge.get("weight") or 1),
                    json.dumps(edge.get("metadata") or {}, ensure_ascii=False, default=str),
                ),
            )
        await db.commit()


async def build_semantic_graph_result(
    *,
    group_id: str,
    target_date: date | None = None,
    keyword: str = "",
    limit: int = DEFAULT_GRAPH_LIMIT,
) -> dict[str, object]:
    safe_limit = clamp_limit(limit)
    scope = "date" if target_date is not None else "recent"
    source_key = target_date.isoformat() if target_date is not None else f"recent:{safe_limit}"
    messages = await fetch_messages_for_graph(
        group_id=group_id,
        target_date=target_date,
        keyword=keyword,
        limit=safe_limit,
    )
    graph = build_graph_from_messages(
        group_id=group_id,
        messages=messages,
        scope=scope,
        source_key=source_key,
        keyword=normalize_text(keyword),
        limit=safe_limit,
    )
    await save_semantic_graph(
        graph,
        params={
            "target_date": target_date.isoformat() if target_date else "",
            "keyword": normalize_text(keyword),
            "limit": safe_limit,
        },
    )
    return graph


def _decode_json(raw: str, fallback: object) -> object:
    try:
        return json.loads(raw or "")
    except Exception:
        return fallback


async def load_semantic_graph(graph_id: str) -> dict[str, object] | None:
    await init_semantic_graph_db()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        graph_cursor = await db.execute(
            """
            SELECT *
            FROM semantic_graphs
            WHERE graph_id = ?
            """,
            (graph_id,),
        )
        graph_row = await graph_cursor.fetchone()
        if not graph_row:
            return None
        node_cursor = await db.execute(
            """
            SELECT label, kind, weight, metadata_json
            FROM semantic_graph_nodes
            WHERE graph_id = ?
            ORDER BY weight DESC, label ASC
            """,
            (graph_id,),
        )
        node_rows = await node_cursor.fetchall()
        edge_cursor = await db.execute(
            """
            SELECT source_label, source_kind, target_label, target_kind, relation, weight, metadata_json
            FROM semantic_graph_edges
            WHERE graph_id = ?
            ORDER BY weight DESC, source_label ASC, target_label ASC
            """,
            (graph_id,),
        )
        edge_rows = await edge_cursor.fetchall()

    return {
        "ok": True,
        "graph_id": str(graph_row["graph_id"]),
        "group_id": str(graph_row["group_id"]),
        "scope": str(graph_row["scope"]),
        "source_key": str(graph_row["source_key"]),
        "title": str(graph_row["title"]),
        "keyword": str(graph_row["keyword"] or ""),
        "message_count": int(graph_row["message_count"] or 0),
        "node_count": int(graph_row["node_count"] or 0),
        "edge_count": int(graph_row["edge_count"] or 0),
        "nodes": [
            {
                "label": str(row["label"]),
                "kind": str(row["kind"]),
                "weight": float(row["weight"] or 1),
                "metadata": _decode_json(str(row["metadata_json"] or "{}"), {}),
            }
            for row in node_rows
        ],
        "edges": [
            {
                "source": str(row["source_label"]),
                "source_kind": str(row["source_kind"]),
                "target": str(row["target_label"]),
                "target_kind": str(row["target_kind"]),
                "relation": str(row["relation"]),
                "weight": float(row["weight"] or 1),
                "metadata": _decode_json(str(row["metadata_json"] or "{}"), {}),
            }
            for row in edge_rows
        ],
        "updated_at": str(graph_row["updated_at"] or ""),
    }


async def latest_semantic_graph(group_id: str) -> dict[str, object] | None:
    await init_semantic_graph_db()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT graph_id
            FROM semantic_graphs
            WHERE group_id = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (str(group_id),),
        )
        row = await cursor.fetchone()
    if not row:
        return None
    return await load_semantic_graph(str(row[0]))


def semantic_graph_summary(graph: dict[str, object], *, max_nodes: int = 10, max_edges: int = 12) -> str:
    nodes = [item for item in graph.get("nodes", []) if isinstance(item, dict)]
    edges = [item for item in graph.get("edges", []) if isinstance(item, dict)]
    top_nodes = nodes[:max_nodes]
    top_edges = edges[:max_edges]
    lines = [
        str(graph.get("title") or "语义图"),
        f"消息数：{graph.get('message_count', 0)}；节点：{graph.get('node_count', len(nodes))}；关系：{graph.get('edge_count', len(edges))}",
    ]
    if top_nodes:
        lines.append("核心节点：" + "、".join(f"{item.get('label')}({item.get('kind')})" for item in top_nodes))
    if top_edges:
        lines.append("主要关系：")
        for edge in top_edges:
            lines.append(
                f"- {edge.get('source')} --{edge.get('relation')}({edge.get('weight')})--> {edge.get('target')}"
            )
    if not nodes:
        lines.append("暂无可用节点。请确认该群已开启消息采集，且所选范围内有消息。")
    return "\n".join(lines)
