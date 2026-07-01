from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from urllib.request import Request, urlopen


DB_PATH = Path("data/companion_memory.db")
REPO_ROOT = Path(__file__).resolve().parent

STS2_DATABASE_CARDS_API = "https://api.github.com/repos/vitechliu/sts2_database/contents/cards?ref=main"
SPIRE_CODEX_RAW_ROOT = "https://raw.githubusercontent.com/ptrlrd/spire-codex/main/data"
SPIRE_CODEX_ZHS_RAW_ROOT = f"{SPIRE_CODEX_RAW_ROOT}/zhs"

DEFAULT_CARD_DIRS = (
    REPO_ROOT / "knowledge_sources" / "sts2_database" / "cards",
    REPO_ROOT.parent / "research-github" / "sts2_sources" / "sts2_database" / "cards",
)
DEFAULT_CODEX_DIRS = (
    REPO_ROOT / "knowledge_sources" / "spire-codex" / "data" / "zhs",
    REPO_ROOT.parent / "research-github" / "sts2_sources" / "spire-codex" / "data" / "zhs",
)
DEFAULT_GUIDE_DIR = REPO_ROOT / "knowledge_sources" / "sts2_guides"

TREE_ORDER = ("STS2",)
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


def fetch_json(url: str):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 HunterBotKnowledgeSeed/2.0"})
    with urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def first_existing_dir(paths: tuple[Path, ...]) -> Path | None:
    for path in paths:
        if path.exists() and path.is_dir():
            return path
    return None


def clean_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def clean_content(value: object) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()).strip() for line in text.split("\n")]
    normalized: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if not blank:
                normalized.append("")
            blank = True
            continue
        normalized.append(line)
        blank = False
    return "\n".join(normalized).strip()


def strip_game_markup(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"\[/?[a-zA-Z0-9_=\-#]+\]", "", text)
    return clean_content(text)


def compact_name(value: str) -> str:
    return re.sub(r"[\s\-_]+", "", value or "").lower()


def unique_keywords(*values: object) -> list[str]:
    keywords: list[str] = []
    for value in values:
        if isinstance(value, dict):
            items = [str(key) for key in value.keys()]
        elif isinstance(value, (list, tuple, set)):
            items = [str(item) for item in value]
        else:
            items = [str(value)]
        for item in items:
            keyword = clean_text(item)
            if keyword and keyword not in keywords:
                keywords.append(keyword[:48])
    return keywords[:16]


def build_title(chinese_name: str, english_name: str) -> str:
    chinese_name = clean_text(chinese_name)
    english_name = clean_text(english_name)
    if chinese_name and english_name and chinese_name != english_name:
        return f"{chinese_name} ({english_name})"
    return chinese_name or english_name


def json_summary(value: object, max_chars: int = 1400) -> str:
    if value in (None, "", [], {}):
        return ""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = strip_game_markup(text)
    if len(text) > max_chars:
        return text[:max_chars].rstrip() + "..."
    return text


def line(label: str, value: object) -> str | None:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, (dict, list)):
        text = json_summary(value)
    else:
        text = strip_game_markup(value)
    if not text:
        return None
    return f"{label}: {text}"


def make_content(*parts: str | None) -> str:
    return clean_content("\n".join(part for part in parts if part))


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
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
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_companion_knowledge_enabled_updated
        ON companion_knowledge_items (enabled, updated_at)
        """
    )


def now_text() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def delete_category_prefixes(conn: sqlite3.Connection, prefixes: list[str]) -> int:
    deleted = 0
    for prefix in prefixes:
        cur = conn.execute(
            """
            SELECT COUNT(*)
            FROM companion_knowledge_items
            WHERE category = ? OR category LIKE ?
            """,
            (prefix, f"{prefix}/%"),
        )
        row = cur.fetchone()
        deleted += int(row[0] or 0) if row else 0
        conn.execute(
            """
            DELETE FROM companion_knowledge_items
            WHERE category = ? OR category LIKE ?
            """,
            (prefix, f"{prefix}/%"),
        )
    return deleted


def insert_items(conn: sqlite3.Connection, items: list[dict[str, object]]) -> int:
    timestamp = now_text()
    inserted = 0
    for item in items:
        title = clean_text(item.get("title"))[:200] or "未命名知识"
        content = clean_content(item.get("content"))[:12000]
        if not content:
            continue
        keywords = unique_keywords(item.get("keywords") or [])
        if not keywords:
            keywords = unique_keywords(title, content)
        category = clean_text(item.get("category"))[:100] or "STS2/guide"
        enabled_value = item.get("enabled", True)
        if isinstance(enabled_value, str):
            enabled = enabled_value.strip().lower() not in {"0", "false", "off", "no", "disabled"}
        else:
            enabled = bool(enabled_value)

        conn.execute(
            """
            INSERT INTO companion_knowledge_items (
                title,
                content,
                keywords,
                category,
                enabled,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                content,
                json.dumps(keywords, ensure_ascii=False),
                category,
                1 if enabled else 0,
                timestamp,
                timestamp,
            ),
        )
        inserted += 1
    return inserted


def load_sts2_database_cards() -> list[dict[str, object]]:
    env_dir = os.getenv("STS2_DATABASE_CARDS_DIR", "").strip()
    candidates = (Path(env_dir),) + DEFAULT_CARD_DIRS if env_dir else DEFAULT_CARD_DIRS
    card_dir = first_existing_dir(candidates)
    if card_dir:
        cards: list[dict[str, object]] = []
        for path in sorted(card_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and isinstance(data.get("card"), dict):
                cards.append(data)
        return cards

    listing = fetch_json(STS2_DATABASE_CARDS_API)
    if not isinstance(listing, list):
        raise RuntimeError("STS2 card source listing is not a list")
    cards = []
    for entry in listing:
        if not isinstance(entry, dict):
            continue
        download_url = str(entry.get("download_url") or "").strip()
        if not download_url:
            continue
        data = fetch_json(download_url)
        if isinstance(data, dict) and isinstance(data.get("card"), dict):
            cards.append(data)
    return cards


def load_codex_json(filename: str, *, localized: bool = True):
    env_dir = os.getenv("SPIRE_CODEX_ZHS_DIR", "").strip()
    candidates = (Path(env_dir),) + DEFAULT_CODEX_DIRS if env_dir else DEFAULT_CODEX_DIRS
    codex_dir = first_existing_dir(candidates)
    if codex_dir:
        path = codex_dir / filename if localized else codex_dir.parent / filename
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    root = SPIRE_CODEX_ZHS_RAW_ROOT if localized else SPIRE_CODEX_RAW_ROOT
    return fetch_json(f"{root}/{filename}")


def build_sts2_card_items() -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for entry in load_sts2_database_cards():
        card = entry.get("card")
        if not isinstance(card, dict):
            continue
        card_id = clean_text(card.get("key"))
        chinese_name = clean_text(card.get("name_chs"))
        english_name = clean_text(card.get("name_eng")) or card_id
        if not card_id and not (chinese_name or english_name):
            continue
        content = make_content(
            "来源: sts2_database (MIT)",
            line("游戏版本", entry.get("game_version")),
            line("数据库版本", entry.get("database_version")),
            "分类: STS2 card",
            line("卡牌ID", card_id),
            line("英文名", english_name),
            line("中文名", chinese_name),
            line("角色", card.get("category")),
            line("费用", card.get("cost")),
            line("稀有度", card.get("rarity")),
            line("类型", card.get("type")),
            line("目标", card.get("targetType")),
            line("中文说明", card.get("text_default_chs") or card.get("text_raw_chs")),
            line("英文说明", card.get("text_raw_eng")),
            line("变量", card.get("variables")),
            line("升级", card.get("upgrades")),
        )
        items.append(
            {
                "title": build_title(chinese_name, english_name),
                "content": content,
                "keywords": unique_keywords(
                    card_id,
                    chinese_name,
                    english_name,
                    compact_name(chinese_name),
                    compact_name(english_name),
                    card.get("category"),
                    card.get("rarity"),
                    card.get("type"),
                    "STS2",
                    "card",
                ),
                "category": "STS2/card",
                "enabled": True,
            }
        )
    return items


def build_sts2_monster_items() -> list[dict[str, object]]:
    monsters = load_codex_json("monsters.json")
    if not isinstance(monsters, list):
        raise RuntimeError("STS2 monsters source is not a list")

    items: list[dict[str, object]] = []
    for entry in monsters:
        if not isinstance(entry, dict):
            continue
        monster_id = clean_text(entry.get("id"))
        name = clean_text(entry.get("name")) or monster_id
        monster_type = clean_text(entry.get("type"))
        lower_type = monster_type.lower()
        if lower_type == "boss":
            kind = "boss"
        elif lower_type == "elite":
            kind = "elite"
        else:
            kind = "enemy"
        moves = []
        for move in entry.get("moves") or []:
            if not isinstance(move, dict):
                continue
            moves.append(
                " / ".join(
                    part
                    for part in (
                        clean_text(move.get("name")),
                        clean_text(move.get("intent")),
                        clean_text(json_summary(move.get("damage"), 220)),
                        clean_text(line("格挡", move.get("block")) or ""),
                    )
                    if part
                )
            )
        encounters = []
        for encounter in entry.get("encounters") or []:
            if not isinstance(encounter, dict):
                continue
            encounters.append(
                " / ".join(
                    part
                    for part in (
                        clean_text(encounter.get("room_type")),
                        clean_text(encounter.get("act")),
                        clean_text(encounter.get("encounter_name")),
                    )
                    if part
                )
            )
        attack_pattern = entry.get("attack_pattern") if isinstance(entry.get("attack_pattern"), dict) else {}
        content = make_content(
            "来源: spire-codex data/zhs",
            f"分类: STS2 {kind}",
            line("英文ID", monster_id),
            line("名称", name),
            line("类型", monster_type),
            line("血量", f"{entry.get('min_hp')}-{entry.get('max_hp') or entry.get('min_hp')}"),
            line("进阶血量", f"{entry.get('min_hp_ascension')}-{entry.get('max_hp_ascension') or entry.get('min_hp_ascension')}"),
            line("遭遇", " ; ".join(bit for bit in encounters if bit)),
            line("招式", " ; ".join(bit for bit in moves if bit)),
            line("固有能力", entry.get("innate_powers")),
            line("行动逻辑", attack_pattern.get("description") or attack_pattern),
        )
        items.append(
            {
                "title": name,
                "content": content,
                "keywords": unique_keywords(monster_id, name, monster_type, kind, "STS2"),
                "category": f"STS2/{kind}",
                "enabled": True,
            }
        )
    return items


def build_sts2_relic_items() -> list[dict[str, object]]:
    relics = load_codex_json("relics.json")
    if not isinstance(relics, list):
        raise RuntimeError("STS2 relics source is not a list")
    items: list[dict[str, object]] = []
    for entry in relics:
        if not isinstance(entry, dict):
            continue
        relic_id = clean_text(entry.get("id"))
        name = clean_text(entry.get("name")) or relic_id
        content = make_content(
            "来源: spire-codex data/zhs",
            "分类: STS2 relic",
            line("英文ID", relic_id),
            line("名称", name),
            line("稀有度", entry.get("rarity")),
            line("池子", entry.get("pool")),
            line("商店价格", entry.get("merchant_price")),
            line("说明", entry.get("description")),
            line("原始说明", entry.get("description_raw")),
            line("注记", entry.get("notes")),
            line("风味文本", entry.get("flavor")),
        )
        items.append(
            {
                "title": name,
                "content": content,
                "keywords": unique_keywords(relic_id, name, entry.get("rarity"), entry.get("pool"), "遗物", "STS2"),
                "category": "STS2/relic",
                "enabled": True,
            }
        )
    return items


def build_sts2_potion_items() -> list[dict[str, object]]:
    potions = load_codex_json("potions.json")
    if not isinstance(potions, list):
        raise RuntimeError("STS2 potions source is not a list")
    items: list[dict[str, object]] = []
    for entry in potions:
        if not isinstance(entry, dict):
            continue
        potion_id = clean_text(entry.get("id"))
        name = clean_text(entry.get("name")) or potion_id
        content = make_content(
            "来源: spire-codex data/zhs",
            "分类: STS2 potion",
            line("英文ID", potion_id),
            line("名称", name),
            line("稀有度", entry.get("rarity")),
            line("池子", entry.get("pool")),
            line("说明", entry.get("description")),
            line("原始说明", entry.get("description_raw")),
        )
        items.append(
            {
                "title": name,
                "content": content,
                "keywords": unique_keywords(potion_id, name, entry.get("rarity"), entry.get("pool"), "药水", "STS2"),
                "category": "STS2/potion",
                "enabled": True,
            }
        )
    return items


def build_sts2_character_items() -> list[dict[str, object]]:
    characters = load_codex_json("characters.json")
    if not isinstance(characters, list):
        raise RuntimeError("STS2 characters source is not a list")
    items: list[dict[str, object]] = []
    for entry in characters:
        if not isinstance(entry, dict):
            continue
        char_id = clean_text(entry.get("id"))
        name = clean_text(entry.get("name")) or char_id
        content = make_content(
            "来源: spire-codex data/zhs",
            "分类: STS2 character",
            line("英文ID", char_id),
            line("名称", name),
            line("说明", entry.get("description")),
            line("初始生命", entry.get("starting_hp")),
            line("初始金币", entry.get("starting_gold")),
            line("初始能量", entry.get("max_energy")),
            line("充能球栏位", entry.get("orb_slots")),
            line("初始牌组", entry.get("starting_deck")),
            line("初始遗物", entry.get("starting_relics")),
            line("解锁条件", entry.get("unlocks_after")),
            line("颜色", entry.get("color")),
        )
        items.append(
            {
                "title": name,
                "content": content,
                "keywords": unique_keywords(char_id, name, entry.get("color"), "角色", "STS2"),
                "category": "STS2/character",
                "enabled": True,
            }
        )
    return items


def build_sts2_event_items() -> list[dict[str, object]]:
    events = load_codex_json("events.json")
    if not isinstance(events, list):
        raise RuntimeError("STS2 events source is not a list")
    items: list[dict[str, object]] = []
    for entry in events:
        if not isinstance(entry, dict):
            continue
        event_id = clean_text(entry.get("id"))
        name = clean_text(entry.get("name")) or event_id
        options = []
        for option in entry.get("options") or []:
            if isinstance(option, dict):
                title = clean_text(option.get("title"))
                description = strip_game_markup(option.get("description"))
                if title or description:
                    options.append(f"{title}: {description}" if title and description else title or description)
        content = make_content(
            "来源: spire-codex data/zhs",
            "分类: STS2 event",
            line("英文ID", event_id),
            line("名称", name),
            line("类型", entry.get("type")),
            line("区域", entry.get("act")),
            line("说明", entry.get("description")),
            line("前置条件", entry.get("preconditions")),
            line("选项", " ; ".join(options)),
        )
        items.append(
            {
                "title": name,
                "content": content,
                "keywords": unique_keywords(event_id, name, entry.get("type"), entry.get("act"), "事件", "STS2"),
                "category": "STS2/event",
                "enabled": True,
            }
        )
    return items


def build_sts2_term_items(filename: str, kind: str, label_text: str) -> list[dict[str, object]]:
    terms = load_codex_json(filename)
    if not isinstance(terms, list):
        raise RuntimeError(f"STS2 {filename} source is not a list")
    items: list[dict[str, object]] = []
    for entry in terms:
        if not isinstance(entry, dict):
            continue
        term_id = clean_text(entry.get("id"))
        name = clean_text(entry.get("name")) or term_id
        content = make_content(
            "来源: spire-codex data/zhs",
            f"分类: STS2 {kind}",
            line("英文ID", term_id),
            line("名称", name),
            line("类型", entry.get("type")),
            line("类别", entry.get("category")),
            line("说明", entry.get("description")),
            line("原始说明", entry.get("description_raw")),
            line("卡牌额外文本", entry.get("extra_card_text")),
            line("适用对象", entry.get("applicable_to")),
            line("叠加", entry.get("is_stackable")),
            line("堆叠类型", entry.get("stack_type")),
        )
        items.append(
            {
                "title": name,
                "content": content,
                "keywords": unique_keywords(term_id, name, entry.get("type"), entry.get("category"), label_text, "STS2"),
                "category": f"STS2/{kind}",
                "enabled": True,
            }
        )
    return items


def build_sts2_mechanic_constant_items() -> list[dict[str, object]]:
    raw = load_codex_json("mechanics_constants.json", localized=False)
    if not isinstance(raw, dict):
        return []
    items: list[dict[str, object]] = []
    for key, value in raw.items():
        title = f"机制常数：{key}"
        content = make_content(
            "来源: spire-codex data/mechanics_constants",
            "分类: STS2 mechanic",
            line("键", key),
            line("数据", value),
        )
        items.append(
            {
                "title": title,
                "content": content,
                "keywords": unique_keywords(key, "机制", "常数", "STS2"),
                "category": "STS2/mechanic",
                "enabled": True,
            }
        )
    return items


def latex_read_group(text: str, start: int) -> tuple[str, int]:
    if start >= len(text) or text[start] != "{":
        return "", start
    depth = 0
    content_start = start + 1
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[content_start:index], index + 1
        index += 1
    return text[content_start:], len(text)


def iter_latex_headings(raw: str) -> list[dict[str, object]]:
    headings: list[dict[str, object]] = []
    pattern = re.compile(r"\\(section|subsection|subsubsection)\*?")
    level_map = {"section": 1, "subsection": 2, "subsubsection": 3}
    for match in pattern.finditer(raw):
        pos = match.end()
        while pos < len(raw) and raw[pos].isspace():
            pos += 1
        if pos < len(raw) and raw[pos] == "[":
            depth = 1
            pos += 1
            while pos < len(raw) and depth:
                if raw[pos] == "\\":
                    pos += 2
                    continue
                if raw[pos] == "[":
                    depth += 1
                elif raw[pos] == "]":
                    depth -= 1
                pos += 1
        while pos < len(raw) and raw[pos].isspace():
            pos += 1
        title, end = latex_read_group(raw, pos)
        if not title:
            continue
        headings.append(
            {
                "level": level_map[match.group(1)],
                "title": latex_to_plain(title),
                "start": match.start(),
                "body_start": end,
            }
        )
    for index, heading in enumerate(headings):
        heading["end"] = headings[index + 1]["start"] if index + 1 < len(headings) else len(raw)
    return headings


def latex_to_plain(raw: str) -> str:
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?<!\\)%.*", "", text)
    text = re.sub(r"\\begin\{(?:document|enumerate|itemize|tabular|tikzpicture|minipage|tcolorbox)[^}]*\}", "\n", text)
    text = re.sub(r"\\end\{(?:document|enumerate|itemize|tabular|tikzpicture|minipage|tcolorbox)[^}]*\}", "\n", text)
    text = re.sub(r"\\(newpage|restoregeometry|maketitle|tableofcontents|thispagestyle|pagenumbering|newgeometry)\b(?:\[[^\]]*\])?(?:\{[^{}]*\})?", "\n", text)
    text = re.sub(r"\\item\b", "\n- ", text)
    text = re.sub(r"\\icon\{([^{}]+)\}", "", text)
    text = re.sub(r"\\rarity\{([^{}]+)\}", "", text)
    text = re.sub(r"\\(?:textbf|textit|emph|hl|small|Large|large|huge|Huge|centerline)\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\(?:textcolor|colorbox)\{[^{}]*\}\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\(?:section|subsection|subsubsection)\*?(?:\[[^\]]*\])?\{([^{}]*)\}", r"\n\1\n", text)
    text = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?(?:\{[^{}]*\})?", " ", text)
    text = text.replace("\\%", "%").replace("\\_", "_").replace("\\&", "&")
    text = text.replace("$", " ")
    text = re.sub(r"[{}]", "", text)
    return clean_content(text)


def split_long_text(text: str, max_chars: int = 9500) -> list[str]:
    text = clean_content(text)
    if len(text) <= max_chars:
        return [text] if text else []
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if current and current_len + len(paragraph) + 2 > max_chars:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0
        if len(paragraph) > max_chars:
            for start in range(0, len(paragraph), max_chars):
                if current:
                    chunks.append("\n\n".join(current))
                    current = []
                    current_len = 0
                chunks.append(paragraph[start : start + max_chars])
            continue
        current.append(paragraph)
        current_len += len(paragraph) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def guide_paths() -> list[Path]:
    paths: list[Path] = []
    env_value = os.getenv("STS2_GUIDE_TEX_PATHS", "").strip()
    if env_value:
        for raw_path in re.split(r"[;|]", env_value):
            path = Path(raw_path.strip())
            if path.exists() and path.is_file() and path.suffix.lower() == ".tex":
                paths.append(path)
    if DEFAULT_GUIDE_DIR.exists():
        paths.extend(sorted(DEFAULT_GUIDE_DIR.glob("*.tex")))
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        resolved = str(path.resolve()).lower()
        if resolved not in seen:
            unique.append(path)
            seen.add(resolved)
    return unique


def build_sts2_guide_items() -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for path in guide_paths():
        raw = path.read_text(encoding="utf-8", errors="ignore")
        doc_title = "硫指导：力求面面俱到的 Slay the Spire II 攻略"
        headings = iter_latex_headings(raw)
        if not headings:
            headings = [{"level": 1, "title": path.stem, "body_start": 0, "end": len(raw)}]
        for heading in headings:
            title = clean_text(heading.get("title")) or path.stem
            if title in {"目录"}:
                continue
            body = raw[int(heading["body_start"]) : int(heading["end"])]
            body_text = latex_to_plain(body)
            if len(body_text) < 40:
                continue
            chunks = split_long_text(body_text)
            for index, chunk in enumerate(chunks, start=1):
                suffix = f"（{index}）" if len(chunks) > 1 else ""
                item_title = f"{doc_title} / {title}{suffix}"
                content = make_content(
                    "来源: 自建攻略库",
                    "分类: STS2 guide",
                    f"源文件: {path.as_posix()}",
                    "作者: 二硫键",
                    "编审: Rosaya / zzz",
                    "版本: v0.10.0 based on public-beta v0.107.1",
                    "知识性质: 作者攻略/软知识，用于提供打法思路，不应被当作官方事实。",
                    f"章节: {title}",
                    "正文:",
                    chunk,
                )
                items.append(
                    {
                        "title": item_title,
                        "content": content,
                        "keywords": unique_keywords(
                            title,
                            doc_title,
                            "二硫键",
                            "Rosaya",
                            "Slay the Spire II",
                            "杀戮尖塔2",
                            "塔2",
                            "攻略",
                        ),
                        "category": "STS2/guide",
                        "enabled": True,
                    }
                )
    return items


def build_seed_items() -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    items.extend(build_sts2_card_items())
    items.extend(build_sts2_character_items())
    items.extend(build_sts2_relic_items())
    items.extend(build_sts2_potion_items())
    items.extend(build_sts2_monster_items())
    items.extend(build_sts2_event_items())
    items.extend(build_sts2_term_items("keywords.json", "keyword", "关键词"))
    items.extend(build_sts2_term_items("glossary.json", "mechanic", "术语"))
    items.extend(build_sts2_term_items("powers.json", "power", "能力"))
    items.extend(build_sts2_term_items("enchantments.json", "enchantment", "附魔"))
    items.extend(build_sts2_mechanic_constant_items())
    items.extend(build_sts2_guide_items())
    return items


def summarize_items(items: list[dict[str, object]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for item in items:
        category = clean_text(item.get("category")) or "未分类"
        summary[category] = summary.get(category, 0) + 1
    return dict(sorted(summary.items()))


def seed_sts_knowledge(db_path: str | Path = DB_PATH) -> dict[str, object]:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    items = build_seed_items()
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_schema(conn)
        deleted = delete_category_prefixes(conn, ["STS1", "STS2"])
        inserted = insert_items(conn, items)
        conn.commit()
    finally:
        conn.close()
    return {
        "deleted": deleted,
        "inserted": inserted,
        "total": len(items),
        "categories": summarize_items(items),
    }


def main() -> None:
    result = seed_sts_knowledge()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
