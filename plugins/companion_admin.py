from __future__ import annotations

import asyncio
import base64
from html.parser import HTMLParser
import html
import ipaddress
import json
import mimetypes
import os
import re
import socket
from io import BytesIO
from urllib.parse import unquote
from urllib.parse import urlparse
from urllib.request import Request as UrlRequest
from urllib.request import build_opener
from urllib.request import HTTPRedirectHandler
from urllib.request import urlopen

import aiosqlite
from dotenv import load_dotenv
from nonebot import get_driver

from plugins.companion_memory import (
    DB_PATH,
    bot_persona_prompt,
    delete_knowledge_item,
    get_knowledge_item,
    get_profile,
    init_companion_memory_db,
    list_knowledge_items,
    now_text,
    save_knowledge_item,
    set_companion_setting,
)


load_dotenv(".env.local")

ADMIN_ROUTE_PREFIX = "/hunterbot/companion-admin"
ADMIN_TOKEN_ENV = "COMPANION_ADMIN_TOKEN"
DEFAULT_EXTRACT_MAX_FILE_MB = 15
DEFAULT_EXTRACT_MAX_TEXT_CHARS = 12000
DEFAULT_EXTRACT_WEB_TIMEOUT_SECONDS = 15
DEFAULT_EXTRACT_PDF_MAX_PAGES = 80
DEFAULT_IMAGE_OCR_MODEL = "qwen-vl-plus"
TEXT_MIME_PREFIXES = ("text/",)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".json", ".log", ".html", ".htm"}
PDF_EXTENSIONS = {".pdf"}
TRUTHY = {"1", "true", "True", "yes", "on"}

driver = get_driver()

try:
    from fastapi import Header, HTTPException, Query, Request
    from fastapi.responses import HTMLResponse, JSONResponse
except Exception:  # pragma: no cover - FastAPI is provided by nonebot2[fastapi].
    Header = None
    HTTPException = None
    HTMLResponse = None
    JSONResponse = None
    Query = None
    Request = None


def admin_token() -> str:
    return os.getenv(ADMIN_TOKEN_ENV, "").strip()


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


def extract_max_file_bytes() -> int:
    return get_int_env("COMPANION_EXTRACT_MAX_FILE_MB", DEFAULT_EXTRACT_MAX_FILE_MB, minimum=1, maximum=100) * 1024 * 1024


def extract_max_text_chars() -> int:
    return get_int_env(
        "COMPANION_EXTRACT_MAX_TEXT_CHARS",
        DEFAULT_EXTRACT_MAX_TEXT_CHARS,
        minimum=1000,
        maximum=100000,
    )


def truncate_extracted_text(text: str) -> str:
    cleaned = re.sub(r"\r\n?", "\n", text)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{4,}", "\n\n\n", cleaned).strip()
    limit = extract_max_text_chars()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + "\n\n[内容过长，已截断]"


def guess_extension(filename: str | None, content_type: str | None = None) -> str:
    suffix = os.path.splitext(filename or "")[1].lower()
    if suffix:
        return suffix
    guessed = mimetypes.guess_extension(content_type or "")
    return guessed.lower() if guessed else ""


def guess_resource_kind(filename: str | None, content_type: str | None) -> str:
    suffix = guess_extension(filename, content_type)
    normalized_type = (content_type or "").split(";")[0].strip().lower()
    if suffix in PDF_EXTENSIONS or normalized_type == "application/pdf":
        return "pdf"
    if suffix in IMAGE_EXTENSIONS or normalized_type.startswith("image/"):
        return "image"
    if suffix in TEXT_EXTENSIONS or normalized_type.startswith(TEXT_MIME_PREFIXES):
        return "text"
    return "text"


def decode_text_bytes(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def extract_plain_text(data: bytes) -> str:
    return truncate_extracted_text(decode_text_bytes(data))


def extract_pdf_text_sync(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("服务器缺少 pypdf，先安装：pip install pypdf") from exc

    reader = PdfReader(BytesIO(data))
    max_pages = get_int_env("COMPANION_EXTRACT_PDF_MAX_PAGES", DEFAULT_EXTRACT_PDF_MAX_PAGES, minimum=1, maximum=500)
    page_texts: list[str] = []
    for index, page in enumerate(reader.pages[:max_pages], start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if text.strip():
            page_texts.append(f"[第 {index} 页]\n{text.strip()}")

    if not page_texts:
        raise RuntimeError("没有从 PDF 里读到文字层；如果是扫描版 PDF，需要先做 OCR。")
    return truncate_extracted_text("\n\n".join(page_texts))


async def extract_pdf_text(data: bytes) -> str:
    return await asyncio.to_thread(extract_pdf_text_sync, data)


async def extract_image_text(data: bytes, content_type: str | None) -> str:
    api_key = os.getenv("IMAGE_VISION_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("图片文字提取需要配置 IMAGE_VISION_API_KEY 或 OPENAI_API_KEY。")

    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise RuntimeError("服务器缺少 openai 包，先安装：pip install openai") from exc

    model = os.getenv("IMAGE_OCR_MODEL", os.getenv("IMAGE_VISION_MODEL", DEFAULT_IMAGE_OCR_MODEL)).strip()
    base_url = os.getenv("IMAGE_VISION_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    timeout_seconds = get_int_env("IMAGE_VISION_TIMEOUT_SECONDS", 45, minimum=1, maximum=180)
    mime_type = (content_type or "image/png").split(";")[0].strip() or "image/png"
    image_url = f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"
    client = AsyncOpenAI(api_key=api_key, base_url=base_url) if base_url else AsyncOpenAI(api_key=api_key)
    response = await asyncio.wait_for(
        client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "请只提取图片中能看清的文字。"
                                "保留标题、编号、列表、表格里的关键文本。"
                                "不要解释，不要总结，不要补充看不清的内容。"
                                "如果没有可读文字，只返回：未识别到文字。"
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
        ),
        timeout=timeout_seconds,
    )
    text = (response.choices[0].message.content or "").strip()
    if not text:
        raise RuntimeError("图片 OCR 没有返回文字。")
    return truncate_extracted_text(text)


def is_public_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    try:
        ip_addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        try:
            resolved = socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        except socket.gaierror:
            return False
        ip_addresses = []
        for item in resolved:
            host = item[4][0]
            try:
                ip_addresses.append(ipaddress.ip_address(host))
            except ValueError:
                continue
    return all(not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved) for ip in ip_addresses)


class ReadableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip_depth = 0
        self.title = ""
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered in {"script", "style", "noscript", "svg"}:
            self.skip_depth += 1
        if lowered == "title":
            self.in_title = True
        if lowered in {"p", "div", "section", "article", "li", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in {"script", "style", "noscript", "svg"} and self.skip_depth:
            self.skip_depth -= 1
        if lowered == "title":
            self.in_title = False
        if lowered in {"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        text = html.unescape(data).strip()
        if not text:
            return
        if self.in_title:
            self.title = (self.title + " " + text).strip()
        self.parts.append(text)
        self.parts.append(" ")

    def readable_text(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def extract_html_text(raw_html: str) -> tuple[str, str]:
    parser = ReadableHTMLParser()
    parser.feed(raw_html)
    return parser.title.strip(), truncate_extracted_text(parser.readable_text())


class SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if not is_public_url(newurl):
            raise RuntimeError("链接跳转到了非公网地址，已拒绝提取。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_url_sync(url: str) -> tuple[bytes, str | None, str | None]:
    timeout_seconds = get_int_env(
        "COMPANION_EXTRACT_WEB_TIMEOUT_SECONDS",
        DEFAULT_EXTRACT_WEB_TIMEOUT_SECONDS,
        minimum=1,
        maximum=60,
    )
    max_bytes = extract_max_file_bytes()
    request = UrlRequest(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 HunterBotAdminTextExtractor/1.0",
            "Accept": "text/html,application/pdf,text/plain,image/*;q=0.8,*/*;q=0.5",
        },
    )
    opener = build_opener(SafeRedirectHandler)
    with opener.open(request, timeout=timeout_seconds) as response:
        final_url = response.geturl()
        if not is_public_url(final_url):
            raise RuntimeError("链接最终地址不是公网 http/https，已拒绝提取。")
        content_type = response.headers.get("content-type")
        content_length = response.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > max_bytes:
                    raise RuntimeError(f"文件超过 {max_bytes // 1024 // 1024} MB 限制。")
            except ValueError:
                pass
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise RuntimeError(f"文件超过 {max_bytes // 1024 // 1024} MB 限制。")
    return data, content_type, final_url


async def fetch_url(url: str) -> tuple[bytes, str | None, str | None]:
    return await asyncio.to_thread(fetch_url_sync, url)


async def extract_text_from_resource(
    *,
    data: bytes,
    filename: str | None,
    content_type: str | None,
) -> dict[str, object]:
    kind = guess_resource_kind(filename, content_type)
    if kind == "pdf":
        text = await extract_pdf_text(data)
    elif kind == "image":
        text = await extract_image_text(data, content_type)
    else:
        raw_text = extract_plain_text(data)
        normalized_type = (content_type or "").split(";")[0].lower()
        suffix = guess_extension(filename, content_type)
        if normalized_type in {"text/html", "application/xhtml+xml"} or suffix in {".html", ".htm"}:
            title, html_text = extract_html_text(raw_text)
            return {
                "kind": "webpage",
                "title": title or os.path.basename(filename or "") or "网页文本",
                "text": html_text,
                "chars": len(html_text),
            }
        text = raw_text
    title = os.path.basename(filename or "").strip() or "提取文本"
    return {"kind": kind, "title": title, "text": text, "chars": len(text)}


def check_token(token: str | None = None, authorization: str | None = None) -> None:
    expected_token = admin_token()
    if not expected_token:
        raise HTTPException(status_code=403, detail=f"{ADMIN_TOKEN_ENV} is not configured")

    provided_token = (token or "").strip()
    if not provided_token and authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            provided_token = value.strip()

    if provided_token != expected_token:
        raise HTTPException(status_code=401, detail="invalid admin token")


def row_to_dict(row: aiosqlite.Row | None) -> dict[str, object] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def normalize_profile_payload(payload: dict[str, object]) -> dict[str, object]:
    topics_value = payload.get("topics", [])
    if isinstance(topics_value, str):
        topics = [item.strip() for item in topics_value.replace("，", ",").split(",") if item.strip()]
    elif isinstance(topics_value, list):
        topics = [str(item).strip() for item in topics_value if str(item).strip()]
    else:
        topics = []

    try:
        confidence = float(payload.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0

    return {
        "summary": str(payload.get("summary") or "").strip()[:1000],
        "current_activity": str(payload.get("current_activity") or "").strip()[:1000],
        "personality_notes": str(payload.get("personality_notes") or "").strip()[:1000],
        "emotional_preferences": str(payload.get("emotional_preferences") or "").strip()[:1000],
        "topics": topics[:20],
        "confidence": min(max(confidence, 0.0), 1.0),
    }


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


async def list_registered_users() -> list[dict[str, object]]:
    await init_companion_memory_db()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                r.group_id,
                r.user_id,
                r.sender_name,
                r.active,
                r.registered_at,
                r.updated_at,
                p.summary,
                p.updated_at AS profile_updated_at
            FROM companion_registrations r
            LEFT JOIN companion_profiles p
              ON p.group_id = r.group_id AND p.user_id = r.user_id
            WHERE r.active = 1
            ORDER BY r.updated_at DESC
            """
        )
        rows = await cursor.fetchall()

    users: list[dict[str, object]] = []
    for row in rows:
        display_name = str(row["sender_name"] or "").strip() or str(row["user_id"])
        users.append(
            {
                "kind": "user",
                "group_id": row["group_id"],
                "user_id": row["user_id"],
                "display_name": display_name,
                "registered_at": row["registered_at"],
                "updated_at": row["updated_at"],
                "profile_updated_at": row["profile_updated_at"],
                "summary": row["summary"] or "",
            }
        )
    return users


async def user_detail(group_id: str, user_id: str) -> dict[str, object]:
    await init_companion_memory_db()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        reg_cursor = await db.execute(
            """
            SELECT group_id, user_id, sender_name, active, registered_at, updated_at, revoked_at
            FROM companion_registrations
            WHERE group_id = ? AND user_id = ?
            """,
            (group_id, user_id),
        )
        registration = await reg_cursor.fetchone()

    profile = await get_profile(group_id, user_id)
    return {
        "registration": row_to_dict(registration),
        "profile": row_to_dict(profile),
    }


async def save_user_profile(group_id: str, user_id: str, payload: dict[str, object]) -> dict[str, object]:
    await init_companion_memory_db()
    profile = normalize_profile_payload(payload)
    timestamp = now_text()
    async with aiosqlite.connect(DB_PATH) as db:
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
    return await user_detail(group_id, user_id)


async def dashboard_state() -> dict[str, object]:
    return {
        "persona": await bot_persona_prompt(),
        "users": await list_registered_users(),
        "knowledge": [row_to_dict(row) for row in await list_knowledge_items()],
    }


async def knowledge_detail(item_id: int) -> dict[str, object]:
    item = await get_knowledge_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="knowledge item not found")
    return {"item": row_to_dict(item)}


async def save_knowledge(payload: dict[str, object], item_id: int | None = None) -> dict[str, object]:
    enabled_value = payload.get("enabled", True)
    if isinstance(enabled_value, str):
        enabled = enabled_value.strip().lower() not in {"0", "false", "off", "no", "disabled"}
    else:
        enabled = bool(enabled_value)
    saved_id = await save_knowledge_item(
        item_id=item_id,
        title=str(payload.get("title") or ""),
        content=str(payload.get("content") or ""),
        keywords=normalize_keywords_payload(payload.get("keywords")),
        category=str(payload.get("category") or ""),
        enabled=enabled,
    )
    return await knowledge_detail(saved_id)


def admin_html() -> str:
    token_placeholder = html.escape(admin_token())
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>猎bot 陪伴画像管理</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #20242a;
      --muted: #6a7280;
      --line: #d8dde5;
      --accent: #1f6feb;
      --accent-soft: #e8f1ff;
      --danger: #a23a3a;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
      font-size: 14px;
    }}
    .app {{
      display: grid;
      grid-template-columns: 320px minmax(0, 1fr);
      min-height: 100vh;
    }}
    aside {{
      border-right: 1px solid var(--line);
      background: var(--panel);
      display: flex;
      flex-direction: column;
      min-width: 0;
    }}
    header {{
      padding: 16px;
      border-bottom: 1px solid var(--line);
    }}
    h1 {{
      margin: 0 0 10px;
      font-size: 18px;
      line-height: 1.3;
    }}
    .token-row {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
    }}
    input, textarea {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--text);
      padding: 9px 10px;
      font: inherit;
      line-height: 1.45;
    }}
    textarea {{
      min-height: 92px;
      resize: vertical;
    }}
    button {{
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--text);
      padding: 8px 12px;
      font: inherit;
      cursor: pointer;
    }}
    button.primary {{
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
    }}
    .list {{
      padding: 10px;
      overflow: auto;
    }}
    .item {{
      width: 100%;
      text-align: left;
      border: 1px solid transparent;
      border-radius: 6px;
      padding: 10px;
      margin-bottom: 6px;
      background: transparent;
    }}
    .item.active {{
      background: var(--accent-soft);
      border-color: #b9d3ff;
    }}
    .item-title {{
      font-weight: 700;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .item-meta {{
      color: var(--muted);
      margin-top: 4px;
      font-size: 12px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    main {{
      min-width: 0;
      padding: 20px;
    }}
    .toolbar {{
      display: flex;
      gap: 8px;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 14px;
    }}
    .title {{
      font-size: 20px;
      font-weight: 700;
    }}
    .status {{
      color: var(--muted);
      min-height: 20px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      max-width: 960px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 14px;
    }}
    .extractor {{
      grid-column: 1 / -1;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fbfcfd;
    }}
    .extract-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: center;
      margin-top: 8px;
    }}
    .extract-row:first-child {{
      margin-top: 0;
    }}
    .extract-actions {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 8px;
    }}
    .extract-preview {{
      min-height: 180px;
      margin-top: 8px;
    }}
    input[type="file"] {{
      padding: 7px 10px;
    }}
    label {{
      display: block;
      font-weight: 700;
      margin-bottom: 6px;
    }}
    .field {{
      margin-bottom: 14px;
    }}
    .wide {{
      grid-column: 1 / -1;
    }}
    .hint {{
      color: var(--muted);
      font-size: 12px;
      margin-top: 5px;
    }}
    .danger {{
      color: var(--danger);
    }}
    @media (max-width: 760px) {{
      .app {{ grid-template-columns: 1fr; }}
      aside {{ min-height: 45vh; border-right: 0; border-bottom: 1px solid var(--line); }}
      .grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="app">
    <aside>
      <header>
        <h1>猎bot 陪伴画像管理</h1>
        <div class="token-row">
          <input id="token" type="password" placeholder="管理令牌" value="{token_placeholder}" />
          <button id="load">刷新</button>
        </div>
      </header>
      <div id="list" class="list"></div>
    </aside>
    <main>
      <div class="toolbar">
        <div>
          <div id="title" class="title">未选择</div>
          <div id="status" class="status"></div>
        </div>
        <button id="save" class="primary">保存</button>
      </div>
      <section id="editor" class="panel"></section>
    </main>
  </div>
  <script>
    const state = {{
      selected: {{ kind: 'persona' }},
      data: null,
      detail: null,
      extracted: null,
    }};
    const $ = (id) => document.getElementById(id);
    const tokenInput = $('token');
    const list = $('list');
    const editor = $('editor');
    const title = $('title');
    const status = $('status');

    function token() {{
      return tokenInput.value.trim();
    }}
    async function api(path, options = {{}}) {{
      const headers = Object.assign({{ 'Authorization': 'Bearer ' + token() }}, options.headers || {{}});
      if (options.body && !(options.body instanceof FormData) && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
      const res = await fetch(path, Object.assign({{}}, options, {{ headers }}));
      if (!res.ok) {{
        const text = await res.text();
        throw new Error(text || res.statusText);
      }}
      return res.json();
    }}
    function setStatus(text, isError = false) {{
      status.textContent = text || '';
      status.className = isError ? 'status danger' : 'status';
    }}
    function itemButton(item, active, onclick) {{
      const button = document.createElement('button');
      button.className = 'item' + (active ? ' active' : '');
      button.onclick = onclick;
      const titleNode = document.createElement('div');
      titleNode.className = 'item-title';
      titleNode.textContent = item.title;
      const metaNode = document.createElement('div');
      metaNode.className = 'item-meta';
      metaNode.textContent = item.meta || '';
      button.append(titleNode, metaNode);
      return button;
    }}
    function renderList() {{
      list.innerHTML = '';
      list.appendChild(itemButton(
        {{ title: 'bot 人设', meta: '全局提示词' }},
        state.selected.kind === 'persona',
        () => {{ state.selected = {{ kind: 'persona' }}; renderEditor(); renderList(); }}
      ));
      list.appendChild(itemButton(
        {{ title: '新建知识', meta: '添加一条知识库内容' }},
        state.selected.kind === 'knowledge-new',
        () => {{
          state.selected = {{ kind: 'knowledge-new' }};
          state.detail = {{ item: {{ title: '', content: '', keywords: '[]', category: '', enabled: 1 }} }};
          state.extracted = null;
          renderEditor();
          renderList();
        }}
      ));
      const knowledgeItems = (state.data && state.data.knowledge) || [];
      for (const item of knowledgeItems) {{
        const active = state.selected.kind === 'knowledge' && state.selected.id === item.id;
        list.appendChild(itemButton(
          {{
            title: '知识：' + item.title,
            meta: `${{item.enabled ? '启用' : '停用'}} / ${{item.category || '未分类'}}`
          }},
          active,
          async () => {{
            state.selected = {{ kind: 'knowledge', id: item.id }};
            state.extracted = null;
            await loadKnowledgeDetail();
            renderList();
          }}
        ));
      }}
      const users = (state.data && state.data.users) || [];
      for (const user of users) {{
        const active = state.selected.kind === 'user'
          && state.selected.group_id === user.group_id
          && state.selected.user_id === user.user_id;
        list.appendChild(itemButton(
          {{
            title: user.display_name,
            meta: `群 ${{user.group_id}} / ${{user.user_id}}`
          }},
          active,
          async () => {{
            state.selected = {{ kind: 'user', group_id: user.group_id, user_id: user.user_id, display_name: user.display_name }};
            state.extracted = null;
            await loadDetail();
            renderList();
          }}
        ));
      }}
    }}
    function field(id, label, value, rows = 4, hint = '') {{
      const wrap = document.createElement('div');
      wrap.className = 'field';
      const labelNode = document.createElement('label');
      labelNode.htmlFor = id;
      labelNode.textContent = label;
      const input = document.createElement(rows > 1 ? 'textarea' : 'input');
      input.id = id;
      if (rows > 1) input.rows = rows;
      input.value = value || '';
      wrap.append(labelNode, input);
      if (hint) {{
        const hintNode = document.createElement('div');
        hintNode.className = 'hint';
        hintNode.textContent = hint;
        wrap.appendChild(hintNode);
      }}
      return wrap;
    }}
    function renderExtractor() {{
      const wrap = document.createElement('div');
      wrap.className = 'extractor';

      const fileRow = document.createElement('div');
      fileRow.className = 'extract-row';
      const fileInput = document.createElement('input');
      fileInput.id = 'extract_file';
      fileInput.type = 'file';
      fileInput.accept = '.pdf,.txt,.md,.html,.htm,.csv,.json,.png,.jpg,.jpeg,.webp,.gif,.bmp,application/pdf,text/*,image/*';
      const fileButton = document.createElement('button');
      fileButton.type = 'button';
      fileButton.textContent = '提取文件文字';
      fileButton.onclick = extractFileText;
      fileRow.append(fileInput, fileButton);

      const urlRow = document.createElement('div');
      urlRow.className = 'extract-row';
      const urlInput = document.createElement('input');
      urlInput.id = 'extract_url';
      urlInput.type = 'url';
      urlInput.placeholder = '粘贴网页、PDF 或图片链接';
      const urlButton = document.createElement('button');
      urlButton.type = 'button';
      urlButton.textContent = '提取网页文字';
      urlButton.onclick = extractUrlText;
      urlRow.append(urlInput, urlButton);

      const hint = document.createElement('div');
      hint.className = 'hint';
      hint.textContent = '只提取文字，不自动总结，也不会自动保存；先预览，再决定追加或替换正文。';

      const previewLabel = document.createElement('label');
      previewLabel.htmlFor = 'extract_preview';
      previewLabel.textContent = '提取预览';
      const preview = document.createElement('textarea');
      preview.id = 'extract_preview';
      preview.className = 'extract-preview';
      preview.rows = 8;
      preview.placeholder = '提取出的文字会先出现在这里。';

      const actions = document.createElement('div');
      actions.className = 'extract-actions';
      const appendButton = document.createElement('button');
      appendButton.type = 'button';
      appendButton.textContent = '追加到正文';
      appendButton.onclick = () => applyExtractedText('append');
      const replaceButton = document.createElement('button');
      replaceButton.type = 'button';
      replaceButton.textContent = '替换正文';
      replaceButton.onclick = () => applyExtractedText('replace');
      const clearButton = document.createElement('button');
      clearButton.type = 'button';
      clearButton.textContent = '清空预览';
      clearButton.onclick = clearExtractPreview;
      actions.append(appendButton, replaceButton, clearButton);

      wrap.append(fileRow, urlRow, hint, previewLabel, preview, actions);
      return wrap;
    }}
    function renderEditor() {{
      editor.innerHTML = '';
      setStatus('');
      if (state.selected.kind === 'persona') {{
        title.textContent = 'bot 人设';
        editor.appendChild(field('persona', '人设提示词', (state.data && state.data.persona) || '', 12, '保存后立即影响后续 AI 回复。'));
        return;
      }}
      if (state.selected.kind === 'knowledge' || state.selected.kind === 'knowledge-new') {{
        const item = (state.detail && state.detail.item) || {{}};
        title.textContent = state.selected.kind === 'knowledge-new' ? '新建知识' : '编辑知识';
        const enabledValue = item.enabled === 0 ? '0' : '1';
        const grid = document.createElement('div');
        grid.className = 'grid';
        grid.appendChild(field('knowledge_title', '标题', item.title || '', 1));
        grid.appendChild(field('knowledge_category', '分类', item.category || '', 1));
        grid.appendChild(field('knowledge_keywords', '关键词', parseTopics(item.keywords).join('，'), 4, '用逗号分隔；留空时会从标题和正文自动提取。'));
        grid.appendChild(field('knowledge_enabled', '启用 1/0', enabledValue, 1));
        grid.appendChild(renderExtractor());
        const content = field('knowledge_content', '正文', item.content || '', 14, '只有当前问题和标题、关键词或正文相关时，bot 才会读取这条内容。');
        content.className += ' wide';
        grid.appendChild(content);
        editor.appendChild(grid);
        if (state.selected.kind === 'knowledge') {{
          const deleteButton = document.createElement('button');
          deleteButton.textContent = '删除这条知识';
          deleteButton.onclick = deleteKnowledge;
          editor.appendChild(deleteButton);
        }}
        return;
      }}
      const detail = state.detail || {{}};
      const profile = detail.profile || {{}};
      const registration = detail.registration || {{}};
      title.textContent = state.selected.display_name || state.selected.user_id;
      setStatus(`群 ${{state.selected.group_id}} / 用户 ${{state.selected.user_id}} / 注册 ${{registration.registered_at || '未知'}}`);
      const grid = document.createElement('div');
      grid.className = 'grid';
      grid.appendChild(field('current_activity', '近期在做', profile.current_activity || '', 4));
      grid.appendChild(field('personality_notes', '互动风格', profile.personality_notes || '', 4));
      grid.appendChild(field('emotional_preferences', '陪伴偏好', profile.emotional_preferences || '', 4));
      grid.appendChild(field('topics', '常聊主题', parseTopics(profile.topics).join('，'), 4, '用逗号分隔。'));
      const summary = field('summary', '画像摘要', profile.summary || '', 6);
      summary.className += ' wide';
      const confidence = field('confidence', '置信度 0-1', profile.confidence ?? '', 1);
      grid.append(summary, confidence);
      editor.appendChild(grid);
    }}
    function parseTopics(value) {{
      if (!value) return [];
      if (Array.isArray(value)) return value;
      try {{
        const parsed = JSON.parse(value);
        return Array.isArray(parsed) ? parsed : [];
      }} catch {{
        return String(value).split(/[，,、\\s]+/).filter(Boolean);
      }}
    }}
    function showExtractedText(result) {{
      const extractedText = (result && result.text) || '';
      if (!extractedText.trim()) {{
        setStatus('没有提取到文字', true);
        return;
      }}
      const titleInput = $('knowledge_title');
      if (titleInput && !titleInput.value.trim() && result.title) {{
        titleInput.value = result.title;
      }}
      const source = result.source_url ? `来源：${{result.source_url}}\\n` : '';
      const preview = $('extract_preview');
      if (preview) preview.value = `${{source}}${{extractedText.trim()}}`;
      state.extracted = result;
      setStatus(`已提取 ${{result.chars || extractedText.length}} 字，确认后追加或替换正文`);
    }}
    function applyExtractedText(mode) {{
      const contentInput = $('knowledge_content');
      const preview = $('extract_preview');
      if (!contentInput || !preview || !preview.value.trim()) {{
        setStatus('没有可应用的提取文字', true);
        return;
      }}
      const block = preview.value.trim();
      if (mode === 'replace') {{
        contentInput.value = block;
        setStatus('已用预览文字替换正文，记得保存知识');
        return;
      }}
      contentInput.value = contentInput.value.trim()
        ? contentInput.value.trim() + '\\n\\n' + block
        : block;
      setStatus('已追加到正文，记得保存知识');
    }}
    function clearExtractPreview() {{
      const preview = $('extract_preview');
      if (preview) preview.value = '';
      state.extracted = null;
      setStatus('预览已清空');
    }}
    function headerSafeText(value) {{
      return encodeURIComponent(value || '');
    }}
    async function extractFileText() {{
      try {{
        const fileInput = $('extract_file');
        const file = fileInput && fileInput.files && fileInput.files[0];
        if (!file) {{
          setStatus('先选择一个文件', true);
          return;
        }}
        setStatus('正在提取文件文字...');
        const result = await api('{ADMIN_ROUTE_PREFIX}/api/extract/file?token=' + encodeURIComponent(token()), {{
          method: 'POST',
          body: file,
          headers: {{
            'Content-Type': file.type || 'application/octet-stream',
            'X-File-Name': headerSafeText(file.name || 'upload')
          }}
        }});
        showExtractedText(result);
      }} catch (error) {{
        setStatus(error.message, true);
      }}
    }}
    async function extractUrlText() {{
      try {{
        const urlInput = $('extract_url');
        const url = urlInput ? urlInput.value.trim() : '';
        if (!url) {{
          setStatus('先粘贴一个链接', true);
          return;
        }}
        setStatus('正在提取网页文字...');
        const result = await api('{ADMIN_ROUTE_PREFIX}/api/extract/url?token=' + encodeURIComponent(token()), {{
          method: 'POST',
          body: JSON.stringify({{ url }})
        }});
        showExtractedText(result);
      }} catch (error) {{
        setStatus(error.message, true);
      }}
    }}
    async function loadDashboard() {{
      setStatus('读取中...');
      state.data = await api('{ADMIN_ROUTE_PREFIX}/api/state?token=' + encodeURIComponent(token()));
      state.detail = null;
      renderList();
      renderEditor();
      setStatus('已读取');
    }}
    async function loadDetail() {{
      setStatus('读取用户画像...');
      state.detail = await api(
        `{ADMIN_ROUTE_PREFIX}/api/users/${{encodeURIComponent(state.selected.group_id)}}/${{encodeURIComponent(state.selected.user_id)}}?token=${{encodeURIComponent(token())}}`
      );
      renderEditor();
      setStatus('已读取用户画像');
    }}
    async function loadKnowledgeDetail() {{
      setStatus('读取知识...');
      state.detail = await api(
        `{ADMIN_ROUTE_PREFIX}/api/knowledge/${{encodeURIComponent(state.selected.id)}}?token=${{encodeURIComponent(token())}}`
      );
      renderEditor();
      setStatus('已读取知识');
    }}
    async function save() {{
      try {{
        setStatus('保存中...');
        if (state.selected.kind === 'persona') {{
          const persona = $('persona').value;
          const result = await api('{ADMIN_ROUTE_PREFIX}/api/persona?token=' + encodeURIComponent(token()), {{
            method: 'PUT',
            body: JSON.stringify({{ persona }})
          }});
          state.data.persona = result.persona;
          renderEditor();
          setStatus('人设已保存');
          return;
        }}
        if (state.selected.kind === 'knowledge' || state.selected.kind === 'knowledge-new') {{
          const payload = {{
            title: $('knowledge_title').value,
            category: $('knowledge_category').value,
            keywords: $('knowledge_keywords').value,
            enabled: $('knowledge_enabled').value,
            content: $('knowledge_content').value
          }};
          const path = state.selected.kind === 'knowledge-new'
            ? '{ADMIN_ROUTE_PREFIX}/api/knowledge?token=' + encodeURIComponent(token())
            : `{ADMIN_ROUTE_PREFIX}/api/knowledge/${{encodeURIComponent(state.selected.id)}}?token=${{encodeURIComponent(token())}}`;
          state.detail = await api(path, {{ method: state.selected.kind === 'knowledge-new' ? 'POST' : 'PUT', body: JSON.stringify(payload) }});
          state.selected = {{ kind: 'knowledge', id: state.detail.item.id }};
          await loadDashboard();
          await loadKnowledgeDetail();
          setStatus('知识已保存');
          return;
        }}
        const payload = {{
          current_activity: $('current_activity').value,
          personality_notes: $('personality_notes').value,
          emotional_preferences: $('emotional_preferences').value,
          topics: $('topics').value,
          summary: $('summary').value,
          confidence: $('confidence').value
        }};
        state.detail = await api(
          `{ADMIN_ROUTE_PREFIX}/api/users/${{encodeURIComponent(state.selected.group_id)}}/${{encodeURIComponent(state.selected.user_id)}}?token=${{encodeURIComponent(token())}}`,
          {{ method: 'PUT', body: JSON.stringify(payload) }}
        );
        await loadDashboard();
        state.selected.kind = 'user';
        await loadDetail();
        setStatus('画像已保存');
      }} catch (error) {{
        setStatus(error.message, true);
      }}
    }}
    async function deleteKnowledge() {{
      if (state.selected.kind !== 'knowledge') return;
      if (!confirm('确定删除这条知识？')) return;
      try {{
        await api(
          `{ADMIN_ROUTE_PREFIX}/api/knowledge/${{encodeURIComponent(state.selected.id)}}?token=${{encodeURIComponent(token())}}`,
          {{ method: 'DELETE' }}
        );
        state.selected = {{ kind: 'persona' }};
        state.detail = null;
        await loadDashboard();
        setStatus('知识已删除');
      }} catch (error) {{
        setStatus(error.message, true);
      }}
    }}
    $('load').onclick = () => loadDashboard().catch((error) => setStatus(error.message, true));
    $('save').onclick = save;
    loadDashboard().catch((error) => setStatus(error.message, true));
  </script>
</body>
</html>"""


server_app = getattr(driver, "server_app", None)
if (
    server_app is not None
    and HTTPException is not None
    and HTMLResponse is not None
    and JSONResponse is not None
):

    @driver.on_startup
    async def startup() -> None:
        await init_companion_memory_db()

    @server_app.get(ADMIN_ROUTE_PREFIX, response_class=HTMLResponse)
    async def companion_admin_page(token: str | None = Query(default=None)) -> HTMLResponse:
        check_token(token=token)
        return HTMLResponse(admin_html())

    @server_app.get(f"{ADMIN_ROUTE_PREFIX}/api/state")
    async def companion_admin_state(
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        return JSONResponse(await dashboard_state())

    @server_app.put(f"{ADMIN_ROUTE_PREFIX}/api/persona")
    async def companion_admin_save_persona(
        request: Request,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        payload = await request.json()
        persona = str(payload.get("persona") or "")
        await set_companion_setting("bot_persona_prompt", persona)
        return JSONResponse({"persona": await bot_persona_prompt()})

    @server_app.get(f"{ADMIN_ROUTE_PREFIX}/api/users/{{group_id}}/{{user_id}}")
    async def companion_admin_user_detail(
        group_id: str,
        user_id: str,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        return JSONResponse(await user_detail(group_id, user_id))

    @server_app.put(f"{ADMIN_ROUTE_PREFIX}/api/users/{{group_id}}/{{user_id}}")
    async def companion_admin_save_user(
        group_id: str,
        user_id: str,
        request: Request,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        payload = await request.json()
        return JSONResponse(await save_user_profile(group_id, user_id, payload))

    @server_app.get(f"{ADMIN_ROUTE_PREFIX}/api/knowledge/{{item_id}}")
    async def companion_admin_knowledge_detail(
        item_id: int,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        return JSONResponse(await knowledge_detail(item_id))

    @server_app.post(f"{ADMIN_ROUTE_PREFIX}/api/knowledge")
    async def companion_admin_create_knowledge(
        request: Request,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        payload = await request.json()
        return JSONResponse(await save_knowledge(payload))

    @server_app.put(f"{ADMIN_ROUTE_PREFIX}/api/knowledge/{{item_id}}")
    async def companion_admin_save_knowledge(
        item_id: int,
        request: Request,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        payload = await request.json()
        return JSONResponse(await save_knowledge(payload, item_id=item_id))

    @server_app.delete(f"{ADMIN_ROUTE_PREFIX}/api/knowledge/{{item_id}}")
    async def companion_admin_delete_knowledge(
        item_id: int,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        await delete_knowledge_item(item_id)
        return JSONResponse({"deleted": True})

    @server_app.post(f"{ADMIN_ROUTE_PREFIX}/api/extract/file")
    async def companion_admin_extract_file(
        request: Request,
        x_file_name: str | None = Header(default=None),
        content_type: str | None = Header(default=None),
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        max_bytes = extract_max_file_bytes()
        chunks: list[bytes] = []
        total_size = 0
        async for chunk in request.stream():
            total_size += len(chunk)
            if total_size > max_bytes:
                raise HTTPException(status_code=400, detail=f"文件超过 {max_bytes // 1024 // 1024} MB 限制")
            chunks.append(chunk)
        data = b"".join(chunks)
        if len(data) > max_bytes:
            raise HTTPException(status_code=400, detail=f"文件超过 {max_bytes // 1024 // 1024} MB 限制")
        filename = unquote(x_file_name or "").strip() or "upload"
        result = await extract_text_from_resource(
            data=data,
            filename=filename,
            content_type=content_type,
        )
        return JSONResponse(result)

    @server_app.post(f"{ADMIN_ROUTE_PREFIX}/api/extract/url")
    async def companion_admin_extract_url(
        request: Request,
        token: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        check_token(token=token, authorization=authorization)
        payload = await request.json()
        url = str(payload.get("url") or "").strip()
        if not url:
            raise HTTPException(status_code=400, detail="请先填写网页链接")
        if not is_public_url(url):
            raise HTTPException(status_code=400, detail="只允许提取公网 http/https 链接")
        data, content_type, final_url = await fetch_url(url)
        result = await extract_text_from_resource(
            data=data,
            filename=os.path.basename(urlparse(final_url or url).path),
            content_type=content_type,
        )
        result["source_url"] = final_url or url
        return JSONResponse(result)
