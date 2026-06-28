from __future__ import annotations

import asyncio
import io
import html
import ipaddress
import json
import os
import re
import socket
import zipfile
from datetime import datetime
from html.parser import HTMLParser
from xml.etree import ElementTree as ET
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

import aiosqlite
from dotenv import load_dotenv
from nonebot import get_driver, on_fullmatch, on_message
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, Message, MessageEvent
from nonebot.log import logger

from plugins.access_control import FEATURE_COLLECTOR, admin_denial, is_group_feature_enabled
from plugins.message_archive import DB_PATH, init_archive_db, render_plain_text


load_dotenv(".env.local")

MEDIA_INSIGHTS_ENABLED_ENV = "MEDIA_INSIGHTS_ENABLED"
MEDIA_INSIGHTS_AUTO_ENABLED_ENV = "MEDIA_INSIGHTS_AUTO_ENABLED"
MEDIA_INSIGHTS_BATCH_SIZE_ENV = "MEDIA_INSIGHTS_BATCH_SIZE"
IMAGE_VISION_MODEL_ENV = "IMAGE_VISION_MODEL"
IMAGE_VISION_ENABLED_ENV = "IMAGE_VISION_ENABLED"
IMAGE_VISION_API_KEY_ENV = "IMAGE_VISION_API_KEY"
IMAGE_VISION_BASE_URL_ENV = "IMAGE_VISION_BASE_URL"
IMAGE_VISION_TIMEOUT_SECONDS_ENV = "IMAGE_VISION_TIMEOUT_SECONDS"
LINK_FETCH_TIMEOUT_SECONDS_ENV = "LINK_FETCH_TIMEOUT_SECONDS"
LINK_FETCH_MAX_BYTES_ENV = "LINK_FETCH_MAX_BYTES"
DEFAULT_IMAGE_VISION_MODEL = "qwen-vl-plus"
DEFAULT_IMAGE_VISION_TIMEOUT_SECONDS = 45
DEFAULT_LINK_FETCH_TIMEOUT_SECONDS = 15
DEFAULT_LINK_FETCH_MAX_BYTES = 1_048_576
IMAGE_ANALYSIS_PROMPT = """请识别这张群聊图片，用中文输出，供群聊日报总结使用。

要求：
1. 如果是截图，提取能看清的文字、数字、表格、标题、链接、聊天要点。
2. 如果是照片或表情包，描述画面内容、可能表达的情绪或含义。
3. 如果涉及待办、结论、争议、时间地点人物，请明确列出。
4. 不要编造看不清的内容；看不清就写“看不清”。
5. 输出尽量简洁，但要保留总结需要的关键信息。"""

INSIGHT_IMAGE = "image"
INSIGHT_EMOJI = "emoji"
INSIGHT_RECORD = "record"
INSIGHT_FORWARD = "forward"
INSIGHT_FILE = "file"
INSIGHT_REPLY = "reply"
INSIGHT_VIDEO = "video"
INSIGHT_LOCATION = "location"
INSIGHT_SHARE = "share"
INSIGHT_CONTACT = "contact"
INSIGHT_POKE = "poke"
INSIGHT_MUSIC = "music"
INSIGHT_ANONYMOUS = "anonymous"
INSIGHT_JSON = "json"
INSIGHT_XML = "xml"
INSIGHT_LINK = "link"
STATUS_PENDING = "pending"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
VISUAL_INSIGHT_TYPES = {INSIGHT_IMAGE, INSIGHT_EMOJI}
URL_PATTERN = re.compile(r"https?://[^\s<>'\"，。！？、；：）)\]】》]+", re.IGNORECASE)
TRAILING_URL_CHARS = ".,!?;:)]}>，。！？、；：）】》"
RISKY_FILE_EXTENSIONS = {
    ".exe",
    ".dll",
    ".bat",
    ".cmd",
    ".ps1",
    ".vbs",
    ".js",
    ".jar",
    ".scr",
    ".msi",
    ".apk",
    ".sh",
    ".py",
    ".reg",
    ".lnk",
}
ARCHIVE_FILE_EXTENSIONS = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"}
TEXT_FILE_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".log", ".ini", ".yaml", ".yml"}
DOCUMENT_FILE_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".pptx"} | TEXT_FILE_EXTENSIONS
IMAGE_FILE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic", ".tif", ".tiff"}
VOICE_TRANSCRIBE_ENABLED_ENV = "VOICE_TRANSCRIBE_ENABLED"
VOICE_TRANSCRIBE_MODEL_ENV = "VOICE_TRANSCRIBE_MODEL"
VOICE_TRANSCRIBE_API_KEY_ENV = "VOICE_TRANSCRIBE_API_KEY"
VOICE_TRANSCRIBE_BASE_URL_ENV = "VOICE_TRANSCRIBE_BASE_URL"
VOICE_TRANSCRIBE_TIMEOUT_SECONDS_ENV = "VOICE_TRANSCRIBE_TIMEOUT_SECONDS"
DEFAULT_VOICE_TRANSCRIBE_MODEL = "whisper-1"
DEFAULT_VOICE_TRANSCRIBE_TIMEOUT_SECONDS = 90

driver = get_driver()

media_insight_status = on_fullmatch(("媒体识别状态", "素材识别状态"), priority=5, block=True)
media_insight_scan = on_fullmatch(("扫描媒体识别", "扫描素材识别"), priority=5, block=True)
auto_media_insight = on_message(priority=31, block=False)
auto_process_lock = asyncio.Lock()


def media_insights_enabled() -> bool:
    return os.getenv(MEDIA_INSIGHTS_ENABLED_ENV, "").strip() in {"1", "true", "True", "yes", "on"}


def auto_media_insights_enabled() -> bool:
    return os.getenv(MEDIA_INSIGHTS_AUTO_ENABLED_ENV, "1").strip() in {"1", "true", "True", "yes", "on"}


def batch_size() -> int:
    raw_value = os.getenv(MEDIA_INSIGHTS_BATCH_SIZE_ENV, "30")
    try:
        value = int(raw_value)
    except ValueError:
        return 30
    return min(max(value, 1), 200)


def image_vision_model() -> str:
    return os.getenv(IMAGE_VISION_MODEL_ENV, DEFAULT_IMAGE_VISION_MODEL).strip() or DEFAULT_IMAGE_VISION_MODEL


def image_vision_enabled() -> bool:
    return os.getenv(IMAGE_VISION_ENABLED_ENV, "").strip() in {"1", "true", "True", "yes", "on"}


def image_vision_api_key() -> str | None:
    return os.getenv(IMAGE_VISION_API_KEY_ENV) or os.getenv("OPENAI_API_KEY")


def image_vision_base_url() -> str | None:
    return os.getenv(IMAGE_VISION_BASE_URL_ENV) or os.getenv("OPENAI_BASE_URL")


def image_vision_timeout_seconds() -> int:
    raw_value = os.getenv(IMAGE_VISION_TIMEOUT_SECONDS_ENV, str(DEFAULT_IMAGE_VISION_TIMEOUT_SECONDS))
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_IMAGE_VISION_TIMEOUT_SECONDS
    return max(1, value)


def link_fetch_timeout_seconds() -> int:
    raw_value = os.getenv(LINK_FETCH_TIMEOUT_SECONDS_ENV, str(DEFAULT_LINK_FETCH_TIMEOUT_SECONDS))
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_LINK_FETCH_TIMEOUT_SECONDS
    return max(1, value)


def link_fetch_max_bytes() -> int:
    raw_value = os.getenv(LINK_FETCH_MAX_BYTES_ENV, str(DEFAULT_LINK_FETCH_MAX_BYTES))
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_LINK_FETCH_MAX_BYTES
    return min(max(value, 16_384), 5_242_880)


def voice_transcribe_enabled() -> bool:
    return os.getenv(VOICE_TRANSCRIBE_ENABLED_ENV, "").strip() in {"1", "true", "True", "yes", "on"}


def voice_transcribe_model() -> str:
    return os.getenv(VOICE_TRANSCRIBE_MODEL_ENV, DEFAULT_VOICE_TRANSCRIBE_MODEL).strip() or DEFAULT_VOICE_TRANSCRIBE_MODEL


def voice_transcribe_api_key() -> str | None:
    return os.getenv(VOICE_TRANSCRIBE_API_KEY_ENV) or os.getenv("OPENAI_API_KEY")


def voice_transcribe_base_url() -> str | None:
    return os.getenv(VOICE_TRANSCRIBE_BASE_URL_ENV) or os.getenv("OPENAI_BASE_URL")


def voice_transcribe_timeout_seconds() -> int:
    raw_value = os.getenv(VOICE_TRANSCRIBE_TIMEOUT_SECONDS_ENV, str(DEFAULT_VOICE_TRANSCRIBE_TIMEOUT_SECONDS))
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_VOICE_TRANSCRIBE_TIMEOUT_SECONDS
    return max(1, value)


async def transcribe_voice_bytes(target_url: str, name: str) -> str:
    api_key = voice_transcribe_api_key()
    if not api_key:
        raise RuntimeError(f"missing {VOICE_TRANSCRIBE_API_KEY_ENV} / OPENAI_API_KEY")

    _final_url, _content_type, body = await fetch_public_url_bytes(target_url)
    if not body:
        raise RuntimeError("empty audio payload")

    from openai import AsyncOpenAI

    base_url = voice_transcribe_base_url()
    client = AsyncOpenAI(api_key=api_key, base_url=base_url) if base_url else AsyncOpenAI(api_key=api_key)
    audio_file = io.BytesIO(body)
    audio_file.name = name or "voice.bin"  # type: ignore[attr-defined]
    response = await asyncio.wait_for(
        client.audio.transcriptions.create(
            model=voice_transcribe_model(),
            file=audio_file,
        ),
        timeout=voice_transcribe_timeout_seconds(),
    )
    text = clean_text(getattr(response, "text", "") or "")
    if not text:
        raise RuntimeError("empty voice transcription response")
    return text


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_segments(raw_message: str) -> list[dict[str, object]]:
    try:
        value = json.loads(raw_message)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def event_segments(event: MessageEvent) -> list[dict[str, object]]:
    segments: list[dict[str, object]] = []
    for segment in event.get_message():
        segments.append({"type": segment.type, "data": dict(segment.data)})
    return segments


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(html.unescape(str(value)).replace("\r", "\n").split())


def truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "..."


def segment_data(segment: dict[str, object]) -> dict[str, object]:
    data = segment.get("data")
    return data if isinstance(data, dict) else {}


def segment_ref(data: dict[str, object]) -> str:
    for key in ("url", "file", "file_id", "id", "summary", "name", "data"):
        value = data.get(key)
        if value:
            return str(value)
    return ""


def segment_url(data: dict[str, object]) -> str | None:
    for key in ("url", "file", "file_id"):
        value = data.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    return None


def file_name_from_data(data: dict[str, object]) -> str:
    for key in ("name", "file_name", "file", "filename", "file_id"):
        value = data.get(key)
        if value:
            return clean_text(str(value))
    return "未知文件"


def file_extension(name: str) -> str:
    match = re.search(r"(\.[A-Za-z0-9]{1,12})(?:$|\?)", name)
    return match.group(1).lower() if match else ""


def fetch_public_url_bytes_sync(target_url: str) -> tuple[str, str, bytes]:
    validate_public_http_url(target_url)
    opener = build_opener(SafeRedirectHandler)
    request = Request(
        target_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            "Accept-Encoding": "identity",
        },
    )
    max_bytes = link_fetch_max_bytes()
    with opener.open(request, timeout=link_fetch_timeout_seconds()) as response:
        final_url = response.geturl()
        validate_public_http_url(final_url)
        content_type = response.headers.get("Content-Type", "")
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            body = body[:max_bytes]
    return final_url, content_type, body


async def fetch_public_url_bytes(target_url: str) -> tuple[str, str, bytes]:
    return await asyncio.to_thread(fetch_public_url_bytes_sync, target_url)


def compact_multiline_text(text: str, limit: int = 2400) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in normalized.split("\n")]
    compacted = "\n".join(line for line in lines if line)
    return truncate_text(compacted, limit)


def extract_text_from_docx_bytes(body: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            xml_bytes = archive.read("word/document.xml")
    except Exception:
        return ""

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return ""

    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", namespace):
        parts = [node.text or "" for node in paragraph.findall(".//w:t", namespace)]
        text = clean_text("".join(parts))
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def extract_text_from_pptx_bytes(body: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            slide_names = sorted(
                name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
            )
            if not slide_names:
                return ""
            namespace = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
            slides: list[str] = []
            for slide_name in slide_names:
                try:
                    root = ET.fromstring(archive.read(slide_name))
                except Exception:
                    continue
                parts = [node.text or "" for node in root.findall(".//a:t", namespace)]
                text = clean_text(" ".join(parts))
                if text:
                    slides.append(text)
            return "\n".join(slides)
    except Exception:
        return ""


def extract_text_from_xlsx_bytes(body: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            namespace = {
                "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
            }

            shared_strings: list[str] = []
            if "xl/sharedStrings.xml" in archive.namelist():
                try:
                    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                    shared_strings = [
                        clean_text("".join(node.itertext()))
                        for node in root.findall(".//main:si", namespace)
                    ]
                except Exception:
                    shared_strings = []

            sheet_names = sorted(
                name for name in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
            )
            if not sheet_names:
                return ""

            sheets: list[str] = []
            for sheet_name in sheet_names:
                try:
                    root = ET.fromstring(archive.read(sheet_name))
                except Exception:
                    continue
                rows: list[str] = []
                for row in root.findall(".//main:row", namespace):
                    cells: list[str] = []
                    for cell in row.findall("main:c", namespace):
                        cell_type = cell.attrib.get("t", "")
                        value = ""
                        if cell_type == "s":
                            value_node = cell.find("main:v", namespace)
                            if value_node is not None and value_node.text and value_node.text.isdigit():
                                index = int(value_node.text)
                                if 0 <= index < len(shared_strings):
                                    value = shared_strings[index]
                        elif cell_type == "inlineStr":
                            value = "".join(node.text or "" for node in cell.findall(".//main:t", namespace))
                        else:
                            value_node = cell.find("main:v", namespace)
                            if value_node is not None and value_node.text:
                                value = value_node.text
                        value = clean_text(value)
                        if value:
                            cells.append(value)
                    if cells:
                        rows.append("\t".join(cells))
                if rows:
                    sheets.append("\n".join(rows))
            return "\n\n".join(sheets)
    except Exception:
        return ""


def extract_text_from_pdf_bytes(body: bytes) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        return ""

    try:
        reader = PdfReader(io.BytesIO(body))
    except Exception:
        return ""

    pages: list[str] = []
    for page in reader.pages:
        try:
            text = clean_text(page.extract_text() or "")
        except Exception:
            text = ""
        if text:
            pages.append(text)
    return "\n".join(pages)


def extract_file_text_preview_sync(url: str, name: str) -> str:
    ext = file_extension(name)
    if ext in RISKY_FILE_EXTENSIONS or ext in ARCHIVE_FILE_EXTENSIONS:
        return ""
    try:
        _final_url, content_type, body = fetch_public_url_bytes_sync(url)
    except Exception:
        return ""

    if ext in TEXT_FILE_EXTENSIONS:
        return compact_multiline_text(decode_response_body(body, content_type), 2400)
    if ext == ".docx":
        return compact_multiline_text(extract_text_from_docx_bytes(body), 2400)
    if ext == ".pptx":
        return compact_multiline_text(extract_text_from_pptx_bytes(body), 2400)
    if ext == ".xlsx":
        return compact_multiline_text(extract_text_from_xlsx_bytes(body), 2400)
    if ext == ".pdf":
        return compact_multiline_text(extract_text_from_pdf_bytes(body), 2400)
    if "text" in content_type.lower():
        return compact_multiline_text(decode_response_body(body, content_type), 2400)
    return ""


async def extract_file_text_preview(url: str, name: str) -> str:
    return await asyncio.to_thread(extract_file_text_preview_sync, url, name)


def summarize_file_segment(data: dict[str, object]) -> str:
    name = file_name_from_data(data)
    ext = file_extension(name)
    size = data.get("size") or data.get("file_size")
    url = segment_url(data)
    lines = ["文件消息", f"文件名：{name}"]
    if size:
        lines.append(f"大小：{size}")
    if ext:
        lines.append(f"类型：{ext}")
    if ext in RISKY_FILE_EXTENSIONS:
        lines.append("安全提示：高风险文件类型，可能是程序、脚本或安装包；bot 不会执行该文件，日报仅记录文件信息。")
    elif ext in ARCHIVE_FILE_EXTENSIONS:
        lines.append("安全提示：压缩包内容未知；bot 不会自动解压执行，日报仅记录文件信息。")
    elif ext in DOCUMENT_FILE_EXTENSIONS:
        lines.append("读取状态：已记录文件信息；如消息段提供可访问 URL，后续可扩展为提取文档文字。")
    else:
        lines.append("读取状态：未知文件类型，已记录文件信息。")
    if url:
        lines.append("文件提供了可访问链接，已进入后台读取流程；日报不展示链接，如需核验请回到群聊原消息查看。")
    return "\n".join(lines)


def summarize_reply_segment(data: dict[str, object]) -> str:
    reply_id = data.get("id") or data.get("message_id") or data.get("messageId") or ""
    qq = data.get("qq") or data.get("user_id") or ""
    sender = data.get("sender") or data.get("nickname") or ""
    sent_at = data.get("time") or data.get("date") or ""
    parts = ["回复消息"]
    if reply_id:
        parts.append(f"引用ID={reply_id}")
    if qq:
        parts.append(f"引用QQ={qq}")
    if sender:
        parts.append(f"引用发送者={clean_text(str(sender))}")
    if sent_at:
        parts.append(f"引用时间={sent_at}")
    return "；".join(parts)


def summarize_video_segment(data: dict[str, object]) -> str:
    parts = ["视频消息"]
    title = clean_text(str(data.get("summary") or data.get("name") or data.get("file_name") or ""))
    url = segment_url(data)
    if title:
        parts.append(f"描述={title}")
    if url:
        parts.append(f"链接={url}")
    return "；".join(parts)


def summarize_location_segment(data: dict[str, object]) -> str:
    parts = ["位置消息"]
    title = clean_text(str(data.get("title") or data.get("name") or ""))
    address = clean_text(str(data.get("address") or data.get("desc") or ""))
    lat = data.get("lat") or data.get("latitude") or ""
    lng = data.get("lng") or data.get("lon") or data.get("longitude") or ""
    if title:
        parts.append(f"地点={title}")
    if address:
        parts.append(f"地址={address}")
    if lat and lng:
        parts.append(f"坐标={lat},{lng}")
    return "；".join(parts)


def summarize_share_segment(data: dict[str, object]) -> str:
    parts = ["分享卡片"]
    title = clean_text(str(data.get("title") or data.get("name") or ""))
    content = clean_text(str(data.get("content") or data.get("summary") or data.get("desc") or ""))
    url = segment_url(data) or clean_text(str(data.get("url") or ""))
    if title:
        parts.append(f"标题={title}")
    if content:
        parts.append(f"摘要={content}")
    if url:
        parts.append(f"链接={url}")
    return "；".join(parts)


def summarize_contact_segment(data: dict[str, object]) -> str:
    parts = ["名片消息"]
    qq = clean_text(str(data.get("qq") or data.get("user_id") or data.get("id") or ""))
    nickname = clean_text(str(data.get("nickname") or data.get("name") or ""))
    if qq:
        parts.append(f"QQ={qq}")
    if nickname:
        parts.append(f"昵称={nickname}")
    return "；".join(parts)


def summarize_poke_segment(data: dict[str, object]) -> str:
    parts = ["戳一戳消息"]
    poke_type = clean_text(str(data.get("type") or data.get("name") or data.get("text") or ""))
    if poke_type:
        parts.append(f"类型={poke_type}")
    return "；".join(parts)


def summarize_music_segment(data: dict[str, object]) -> str:
    parts = ["音乐分享"]
    title = clean_text(str(data.get("title") or data.get("name") or ""))
    url = segment_url(data) or clean_text(str(data.get("url") or ""))
    if title:
        parts.append(f"标题={title}")
    if url:
        parts.append(f"链接={url}")
    return "；".join(parts)


def summarize_anonymous_segment(data: dict[str, object]) -> str:
    parts = ["匿名消息"]
    nickname = clean_text(str(data.get("nickname") or data.get("name") or ""))
    if nickname:
        parts.append(f"显示名={nickname}")
    return "；".join(parts)


def summarize_record_segment(data: dict[str, object]) -> str:
    parts = ["语音消息"]
    url = segment_url(data) or clean_text(str(data.get("url") or data.get("file") or data.get("file_id") or ""))
    if url:
        parts.append(f"链接={url}")
    if voice_transcribe_enabled():
        parts.append("转写状态=待后台处理")
    else:
        parts.append("转写状态=未开启语音转写")
    return "；".join(parts)


def extract_voice_url(raw_result: str | None) -> str | None:
    payload = extract_segment_payload(raw_result)
    if not payload:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    return segment_url(data) or clean_text(str(data.get("url") or data.get("file") or data.get("file_id") or ""))


def is_sticker_image(data: dict[str, object]) -> bool:
    summary = clean_text(str(data.get("summary", "") or data.get("sub_type", "") or data.get("type", "")))
    file_name = clean_text(str(data.get("file", "") or data.get("file_id", "")))
    marker_text = f"{summary} {file_name}".lower()
    return any(marker in marker_text for marker in ("表情", "动画", "mface", "sticker", "face"))


def normalize_url(url: str) -> str:
    return url.strip().rstrip(TRAILING_URL_CHARS)


def extract_urls_from_text(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in URL_PATTERN.finditer(text):
        url = normalize_url(match.group(0))
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def collect_card_strings(value: object, results: list[str]) -> None:
    if len(results) >= 8:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).lower()
            if normalized_key in {
                "title",
                "desc",
                "description",
                "summary",
                "content",
                "prompt",
                "text",
                "nickname",
                "source",
            }:
                item_text = clean_text(str(item))
                if item_text and item_text not in results and not item_text.startswith("http"):
                    results.append(truncate_text(item_text, 240))
            collect_card_strings(item, results)
    elif isinstance(value, list):
        for item in value:
            collect_card_strings(item, results)


def card_summary_from_text(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""

    snippets: list[str] = []
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = None
    if parsed is not None:
        collect_card_strings(parsed, snippets)

    if not snippets:
        title_match = re.search(r"<title[^>]*>(.*?)</title>", stripped, re.IGNORECASE | re.DOTALL)
        if title_match:
            snippets.append(clean_text(title_match.group(1)))
        meta_match = re.search(
            r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]+content=["\'](.*?)["\']',
            stripped,
            re.IGNORECASE | re.DOTALL,
        )
        if meta_match:
            snippets.append(clean_text(meta_match.group(1)))

    if not snippets:
        compact = clean_text(stripped)
        if compact and len(compact) <= 500:
            snippets.append(compact)

    return "\n".join(dict.fromkeys(snippets))


def link_candidates_from_segment(segment: dict[str, object]) -> list[dict[str, object]]:
    segment_type = str(segment.get("type", ""))
    data = segment_data(segment)
    source_text = ""

    if segment_type == "text":
        source_text = str(data.get("text", "") or "")
    elif segment_type in {INSIGHT_JSON, INSIGHT_XML, "json", "xml"}:
        source_text = str(data.get("data", "") or "")

    if not source_text:
        return []

    card_summary = card_summary_from_text(source_text)
    candidates: list[dict[str, object]] = []
    for url in extract_urls_from_text(source_text):
        candidates.append(
            {
                "url": url,
                "source_type": segment_type,
                "card_summary": card_summary,
                "source_text": truncate_text(clean_text(source_text), 1000),
                "segment": segment,
            }
        )
    return candidates


def build_insights_from_segment(
    segment_index: int,
    segment: dict[str, object],
) -> list[tuple[str, str, str, int, str, object]]:
    segment_type = str(segment.get("type", ""))
    data = segment_data(segment)
    ref = segment_ref(data)
    insights: list[tuple[str, str, str, int, str, object]] = []

    if segment_type == "image":
        insight_type = INSIGHT_EMOJI if is_sticker_image(data) else INSIGHT_IMAGE
        label = "表情包" if insight_type == INSIGHT_EMOJI else "图片"
        insights.append((insight_type, STATUS_PENDING, f"{label}待识别：{ref}", segment_index, str(segment_index), segment))
    elif segment_type == "mface":
        if segment_url(data):
            insights.append(
                (INSIGHT_EMOJI, STATUS_PENDING, f"表情包待识别：{ref}", segment_index, str(segment_index), segment)
            )
        else:
            insights.append((INSIGHT_EMOJI, STATUS_READY, f"表情包：{ref or '无可用摘要'}", segment_index, str(segment_index), segment))
    elif segment_type == "face":
        insights.append((INSIGHT_EMOJI, STATUS_READY, f"QQ表情：{ref or '未知ID'}", segment_index, str(segment_index), segment))
    elif segment_type in {"dice", "rps"}:
        insights.append((INSIGHT_EMOJI, STATUS_READY, f"互动表情：{segment_type} {ref}", segment_index, str(segment_index), segment))
    elif segment_type == "record":
        insights.append((INSIGHT_RECORD, STATUS_PENDING, summarize_record_segment(data), segment_index, str(segment_index), segment))
    elif segment_type == "forward":
        insights.append((INSIGHT_FORWARD, STATUS_PENDING, f"聊天记录待展开：{ref}", segment_index, str(segment_index), segment))
    elif segment_type == "file":
        insights.append(
            (
                INSIGHT_FILE,
                STATUS_PENDING,
                summarize_file_segment(data),
                segment_index,
                str(segment_index),
                segment,
            )
        )
    elif segment_type == "reply":
        insights.append((INSIGHT_REPLY, STATUS_READY, summarize_reply_segment(data), segment_index, str(segment_index), segment))
    elif segment_type == "video":
        insights.append((INSIGHT_VIDEO, STATUS_READY, summarize_video_segment(data), segment_index, str(segment_index), segment))
    elif segment_type == "location":
        insights.append((INSIGHT_LOCATION, STATUS_READY, summarize_location_segment(data), segment_index, str(segment_index), segment))
    elif segment_type == "share":
        insights.append((INSIGHT_SHARE, STATUS_READY, summarize_share_segment(data), segment_index, str(segment_index), segment))
    elif segment_type == "contact":
        insights.append((INSIGHT_CONTACT, STATUS_READY, summarize_contact_segment(data), segment_index, str(segment_index), segment))
    elif segment_type == "poke":
        insights.append((INSIGHT_POKE, STATUS_READY, summarize_poke_segment(data), segment_index, str(segment_index), segment))
    elif segment_type == "music":
        insights.append((INSIGHT_MUSIC, STATUS_READY, summarize_music_segment(data), segment_index, str(segment_index), segment))
    elif segment_type == "anonymous":
        insights.append((INSIGHT_ANONYMOUS, STATUS_READY, summarize_anonymous_segment(data), segment_index, str(segment_index), segment))
    elif segment_type == "json":
        insights.append((INSIGHT_JSON, STATUS_READY, f"JSON消息：{ref}", segment_index, str(segment_index), segment))
    elif segment_type == "xml":
        insights.append((INSIGHT_XML, STATUS_READY, f"XML消息：{ref}", segment_index, str(segment_index), segment))

    for link_index, candidate in enumerate(link_candidates_from_segment(segment)):
        url = str(candidate["url"])
        insights.append(
            (
                INSIGHT_LINK,
                STATUS_PENDING,
                f"链接待解析：{url}",
                segment_index,
                f"{segment_index}:{link_index}",
                candidate,
            )
        )

    return insights


def build_insights_for_segments(segments: list[dict[str, object]]) -> list[tuple[str, str, str, int, str, object]]:
    insights: list[tuple[str, str, str, int, str, object]] = []
    for segment_index, segment in enumerate(segments):
        insights.extend(build_insights_from_segment(segment_index, segment))
    return insights


def has_insight_segments(segments: list[dict[str, object]]) -> bool:
    return any(build_insights_from_segment(index, segment) for index, segment in enumerate(segments))


def extract_image_url(raw_result: str | None) -> str | None:
    if not raw_result:
        return None
    try:
        value = json.loads(raw_result)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    data = value.get("data")
    if not isinstance(data, dict):
        return None
    for key in ("url", "file", "file_id"):
        image_url = data.get(key)
        if isinstance(image_url, str) and image_url.startswith(("http://", "https://")):
            return image_url
    return None


def extract_segment_payload(raw_result: str | None) -> dict[str, object] | None:
    if not raw_result:
        return None
    try:
        value = json.loads(raw_result)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def check_url_accessible_sync(image_url: str, timeout: int) -> None:
    headers = {"User-Agent": "Mozilla/5.0"}
    request = Request(image_url, method="HEAD", headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status >= 400:
                raise URLError(f"HTTP {response.status}")
            return
    except Exception:
        request = Request(image_url, headers={**headers, "Range": "bytes=0-0"})
        with urlopen(request, timeout=timeout) as response:
            if response.status >= 400:
                raise URLError(f"HTTP {response.status}")
            response.read(1)


async def check_url_accessible(image_url: str) -> None:
    await asyncio.to_thread(check_url_accessible_sync, image_url, min(image_vision_timeout_seconds(), 15))


async def analyze_image_url(image_url: str) -> str:
    api_key = image_vision_api_key()
    if not api_key:
        raise RuntimeError(f"missing {IMAGE_VISION_API_KEY_ENV} or OPENAI_API_KEY")

    from openai import AsyncOpenAI

    base_url = image_vision_base_url()
    client = AsyncOpenAI(api_key=api_key, base_url=base_url) if base_url else AsyncOpenAI(api_key=api_key)
    response = await asyncio.wait_for(
        client.chat.completions.create(
            model=image_vision_model(),
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": IMAGE_ANALYSIS_PROMPT},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
        ),
        timeout=image_vision_timeout_seconds(),
    )
    content = response.choices[0].message.content or ""
    content = content.strip()
    if not content:
        raise RuntimeError("empty image analysis response")
    return content


def validate_public_http_url(target_url: str) -> None:
    parsed = urlparse(target_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("only http/https URLs are supported")
    if not parsed.hostname:
        raise ValueError("missing URL host")

    hostname = parsed.hostname.strip().lower()
    if hostname in {"localhost", "localhost.localdomain"}:
        raise ValueError("local URLs are not allowed")

    try:
        host_ip = ipaddress.ip_address(hostname)
        addresses = [host_ip]
    except ValueError:
        try:
            addr_infos = socket.getaddrinfo(hostname, parsed.port or None, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ValueError(f"cannot resolve URL host: {hostname}") from exc
        addresses = [ipaddress.ip_address(info[4][0]) for info in addr_infos]

    for address in addresses:
        if not address.is_global:
            raise ValueError("non-public URLs are not allowed")


class SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        validate_public_http_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class PageTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.text_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_map = {name.lower(): value or "" for name, value in attrs}
        if tag in {"script", "style", "svg", "canvas", "noscript"}:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "meta":
            key = (attrs_map.get("name") or attrs_map.get("property") or "").lower()
            content = clean_text(attrs_map.get("content", ""))
            if key and content:
                self.meta[key] = content

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "svg", "canvas", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        text = clean_text(data)
        if not text:
            return
        if self._in_title:
            self.title_parts.append(text)
            return
        if self._skip_depth:
            return
        if len(text) >= 2:
            self.text_parts.append(text)


def decode_response_body(body: bytes, content_type: str) -> str:
    charset_match = re.search(r"charset=([\w.-]+)", content_type, re.IGNORECASE)
    charsets = [charset_match.group(1)] if charset_match else []
    meta_match = re.search(br"<meta[^>]+charset=[\"']?([\w.-]+)", body[:4096], re.IGNORECASE)
    if meta_match:
        charsets.append(meta_match.group(1).decode("ascii", errors="ignore"))
    charsets.extend(["utf-8", "gb18030"])

    for charset in dict.fromkeys(charsets):
        try:
            return body.decode(charset, errors="replace")
        except LookupError:
            continue
    return body.decode("utf-8", errors="replace")


def fetch_url_text_sync(target_url: str) -> tuple[str, str, str]:
    validate_public_http_url(target_url)
    opener = build_opener(SafeRedirectHandler)
    request = Request(
        target_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.7",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            "Accept-Encoding": "identity",
        },
    )
    max_bytes = link_fetch_max_bytes()
    with opener.open(request, timeout=link_fetch_timeout_seconds()) as response:
        final_url = response.geturl()
        validate_public_http_url(final_url)
        content_type = response.headers.get("Content-Type", "")
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            body = body[:max_bytes]
    return final_url, content_type, decode_response_body(body, content_type)


async def fetch_url_text(target_url: str) -> tuple[str, str, str]:
    return await asyncio.to_thread(fetch_url_text_sync, target_url)


def dedupe_text_parts(parts: list[str], limit: int = 80) -> list[str]:
    results: list[str] = []
    seen: set[str] = set()
    for part in parts:
        text = clean_text(part)
        if not text or text in seen:
            continue
        if len(text) < 2:
            continue
        seen.add(text)
        results.append(text)
        if len(results) >= limit:
            break
    return results


def summarize_html(final_url: str, content_type: str, page_text: str, card_summary: str = "") -> str:
    if "html" not in content_type.lower() and "<html" not in page_text[:500].lower():
        plain_text = truncate_text(clean_text(page_text), 2000)
        if not plain_text:
            raise ValueError("empty non-html response")
        return "\n".join(["链接内容", f"URL：{final_url}", f"正文摘录：{plain_text}"])

    parser = PageTextExtractor()
    parser.feed(page_text)
    title = clean_text(" ".join(parser.title_parts)) or parser.meta.get("og:title", "") or parser.meta.get("twitter:title", "")
    description = (
        parser.meta.get("description", "")
        or parser.meta.get("og:description", "")
        or parser.meta.get("twitter:description", "")
    )
    body_parts = dedupe_text_parts(parser.text_parts)

    skipped = {title, description}
    body = "\n".join(part for part in body_parts if part not in skipped)
    lines = ["链接内容", f"URL：{final_url}"]
    if title:
        lines.append(f"标题：{truncate_text(title, 300)}")
    if description:
        lines.append(f"摘要：{truncate_text(description, 600)}")
    if card_summary:
        lines.append(f"卡片信息：{truncate_text(card_summary, 800)}")
    if body:
        lines.append("正文摘录：")
        lines.append(truncate_text(body, 2600))
    if len(lines) <= 2:
        raise ValueError("no readable page text")
    return "\n".join(lines)


def extract_link_payload(raw_result: str | None) -> dict[str, object] | None:
    if not raw_result:
        return None
    try:
        value = json.loads(raw_result)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    url = value.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    return value


async def analyze_link_payload(payload: dict[str, object]) -> str:
    target_url = str(payload["url"])
    card_summary = clean_text(str(payload.get("card_summary", "") or ""))
    try:
        final_url, content_type, page_text = await fetch_url_text(target_url)
    except Exception:
        if card_summary:
            return "\n".join(["链接卡片信息", f"URL：{target_url}", truncate_text(card_summary, 1200)])
        raise
    return summarize_html(final_url, content_type, page_text, card_summary)


async def upsert_insight(
    *,
    message_archive_id: int,
    group_id: str,
    insight_type: str,
    segment_index: int = 0,
    insight_key: str = "0",
    status: str,
    content: str,
    raw_result: object,
    error: str | None = None,
    replace_existing: bool = True,
) -> None:
    timestamp = now_text()
    values = (
        message_archive_id,
        group_id,
        insight_type,
        segment_index,
        insight_key,
        status,
        content,
        json.dumps(raw_result, ensure_ascii=False, default=str),
        error,
        timestamp,
        timestamp,
    )
    async with aiosqlite.connect(DB_PATH) as db:
        if replace_existing:
            await db.execute(
                """
                INSERT INTO message_insights (
                    message_archive_id,
                    group_id,
                    insight_type,
                    segment_index,
                    insight_key,
                    status,
                    content,
                    raw_result,
                    error,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_archive_id, insight_type, insight_key) DO UPDATE SET
                    segment_index = excluded.segment_index,
                    status = excluded.status,
                    content = excluded.content,
                    raw_result = excluded.raw_result,
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                values,
            )
        else:
            await db.execute(
                """
                INSERT OR IGNORE INTO message_insights (
                    message_archive_id,
                    group_id,
                    insight_type,
                    segment_index,
                    insight_key,
                    status,
                    content,
                    raw_result,
                    error,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        await db.commit()


async def scan_message_row(row: aiosqlite.Row) -> int:
    insight_count = 0
    segments = load_segments(row["raw_message"])
    for insight_type, status, content, segment_index, insight_key, raw_result in build_insights_for_segments(segments):
        await upsert_insight(
            message_archive_id=int(row["id"]),
            group_id=str(row["group_id"]),
            insight_type=insight_type,
            segment_index=segment_index,
            insight_key=insight_key,
            status=status,
            content=content,
            raw_result=raw_result,
            replace_existing=False,
        )
        insight_count += 1
    return insight_count


async def find_collected_message(group_id: str, message_id: str) -> aiosqlite.Row | None:
    if not message_id:
        return None

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, group_id, raw_message
            FROM collected_messages
            WHERE group_id = ? AND message_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (group_id, message_id),
        )
        return await cursor.fetchone()


async def wait_for_collected_message(group_id: str, message_id: str) -> aiosqlite.Row | None:
    for _ in range(10):
        row = await find_collected_message(group_id, message_id)
        if row is not None:
            return row
        await asyncio.sleep(0.2)
    return None


async def auto_process_collected_message(bot: Bot, group_id: str, message_id: str) -> None:
    if not media_insights_enabled() or not auto_media_insights_enabled():
        return

    async with auto_process_lock:
        try:
            row = await wait_for_collected_message(group_id, message_id)
            if row is None:
                return
            await scan_message_row(row)
            await process_pending_images(group_id=group_id)
            await process_pending_links(group_id=group_id)
            await process_pending_files(group_id=group_id)
            await process_pending_record_transcripts(group_id=group_id)
            await process_pending_forwards(bot, group_id=group_id)
        except Exception:
            logger.exception("Auto media insight failed")


async def scan_recent_messages(limit: int | None = None, group_id: str | None = None) -> tuple[int, int]:
    await init_archive_db()
    scan_limit = limit or batch_size()
    where_clause = "WHERE group_id = ?" if group_id else ""
    params: tuple[object, ...] = (group_id, scan_limit) if group_id else (scan_limit,)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT id, group_id, raw_message
            FROM collected_messages
            {where_clause}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        )
        rows = await cursor.fetchall()

    scanned_count = 0
    insight_count = 0
    for row in rows:
        scanned_count += 1
        insight_count += await scan_message_row(row)

    return scanned_count, insight_count


async def process_pending_images(limit: int | None = None, group_id: str | None = None) -> tuple[int, int, int]:
    await init_archive_db()
    process_limit = limit or batch_size()
    group_filter = "AND group_id = ?" if group_id else ""
    params: tuple[object, ...] = (
        INSIGHT_IMAGE,
        INSIGHT_EMOJI,
        STATUS_PENDING,
        group_id,
        process_limit,
    ) if group_id else (
        INSIGHT_IMAGE,
        INSIGHT_EMOJI,
        STATUS_PENDING,
        process_limit,
    )

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT id, message_archive_id, group_id, insight_type, raw_result
            , segment_index, insight_key
            FROM message_insights
            WHERE insight_type IN (?, ?) AND status = ?
            {group_filter}
            ORDER BY id ASC
            LIMIT ?
            """,
            params,
        )
        rows = await cursor.fetchall()

    ready_count = 0
    failed_count = 0
    skipped_count = 0
    for row in rows:
        visual_label = "表情包" if row["insight_type"] == INSIGHT_EMOJI else "图片"
        if not image_vision_enabled():
            skipped_count += 1
            continue

        image_url = extract_image_url(row["raw_result"])
        if not image_url:
            await upsert_insight(
                message_archive_id=int(row["message_archive_id"]),
                group_id=str(row["group_id"]),
                insight_type=str(row["insight_type"]),
                segment_index=int(row["segment_index"] or 0),
                insight_key=str(row["insight_key"] or "0"),
                status=STATUS_FAILED,
                content=f"{visual_label}识别失败：没有可访问的URL。",
                raw_result={"previous_raw_result": row["raw_result"]},
                error="missing visual url",
            )
            failed_count += 1
            continue

        if not image_vision_api_key():
            skipped_count += 1
            continue

        try:
            await check_url_accessible(image_url)
            analysis = await analyze_image_url(image_url)
        except Exception as exc:
            logger.exception("Image insight failed")
            await upsert_insight(
                message_archive_id=int(row["message_archive_id"]),
                group_id=str(row["group_id"]),
                insight_type=str(row["insight_type"]),
                segment_index=int(row["segment_index"] or 0),
                insight_key=str(row["insight_key"] or "0"),
                status=STATUS_FAILED,
                content=f"{visual_label}识别失败：{exc}",
                raw_result={"image_url": image_url},
                error=str(exc),
            )
            failed_count += 1
            continue

        await upsert_insight(
            message_archive_id=int(row["message_archive_id"]),
            group_id=str(row["group_id"]),
            insight_type=str(row["insight_type"]),
            segment_index=int(row["segment_index"] or 0),
            insight_key=str(row["insight_key"] or "0"),
            status=STATUS_READY,
            content=analysis,
            raw_result={"image_url": image_url, "model": image_vision_model()},
        )
        ready_count += 1

    return ready_count, failed_count, skipped_count


async def process_pending_files(limit: int | None = None, group_id: str | None = None) -> tuple[int, int, int]:
    await init_archive_db()
    process_limit = limit or batch_size()
    group_filter = "AND group_id = ?" if group_id else ""
    params: tuple[object, ...] = (
        INSIGHT_FILE,
        STATUS_PENDING,
        group_id,
        process_limit,
    ) if group_id else (
        INSIGHT_FILE,
        STATUS_READY,
        process_limit,
    )

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT id, message_archive_id, group_id, content, raw_result, segment_index, insight_key
            FROM message_insights
            WHERE insight_type = ? AND status = ?
            {group_filter}
            ORDER BY id ASC
            LIMIT ?
            """,
            params,
        )
        rows = await cursor.fetchall()

    ready_count = 0
    failed_count = 0
    skipped_count = 0
    for row in rows:
        payload = extract_segment_payload(row["raw_result"])
        if payload is None:
            skipped_count += 1
            continue
        data = payload.get("data")
        if not isinstance(data, dict):
            skipped_count += 1
            continue
        url = segment_url(data)
        name = file_name_from_data(data)
        if not url:
            skipped_count += 1
            continue
        try:
            preview = await extract_file_text_preview(url, name)
        except Exception as exc:
            logger.exception("File insight failed")
            await upsert_insight(
                message_archive_id=int(row["message_archive_id"]),
                group_id=str(row["group_id"]),
                insight_type=INSIGHT_FILE,
                segment_index=int(row["segment_index"] or 0),
                insight_key=str(row["insight_key"] or "0"),
                status=STATUS_FAILED,
                content=f"文件内容读取失败：{exc}",
                raw_result={"url": url, "name": name},
                error=str(exc),
            )
            failed_count += 1
            continue

        if not preview:
            skipped_count += 1
            continue

        content = "\n".join(
            [
                "文件内容摘录",
                f"文件名：{name}",
                f"内容：{preview}",
            ]
        )
        await upsert_insight(
            message_archive_id=int(row["message_archive_id"]),
            group_id=str(row["group_id"]),
            insight_type=INSIGHT_FILE,
            segment_index=int(row["segment_index"] or 0),
            insight_key=str(row["insight_key"] or "0"),
            status=STATUS_READY,
            content=content,
            raw_result={"url": url, "name": name, "preview": preview},
        )
        ready_count += 1

    return ready_count, failed_count, skipped_count


async def process_pending_record_transcripts(limit: int | None = None, group_id: str | None = None) -> tuple[int, int, int]:
    await init_archive_db()
    process_limit = limit or batch_size()
    group_filter = "AND group_id = ?" if group_id else ""
    params: tuple[object, ...] = (
        INSIGHT_RECORD,
        STATUS_PENDING,
        group_id,
        process_limit,
    ) if group_id else (
        INSIGHT_RECORD,
        STATUS_READY,
        process_limit,
    )

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT id, message_archive_id, group_id, content, raw_result, segment_index, insight_key
            FROM message_insights
            WHERE insight_type = ? AND status = ?
            {group_filter}
            ORDER BY id ASC
            LIMIT ?
            """,
            params,
        )
        rows = await cursor.fetchall()

    ready_count = 0
    failed_count = 0
    skipped_count = 0
    for row in rows:
        payload = extract_segment_payload(row["raw_result"])
        if payload is None:
            skipped_count += 1
            continue
        data = payload.get("data")
        if not isinstance(data, dict):
            skipped_count += 1
            continue
        url = extract_voice_url(row["raw_result"]) or segment_url(data)
        name = file_name_from_data(data) or "voice.mp3"
        if not url:
            skipped_count += 1
            continue
        if not voice_transcribe_enabled():
            skipped_count += 1
            continue
        try:
            text = await transcribe_voice_bytes(url, name)
        except Exception as exc:
            logger.exception("Voice insight failed")
            await upsert_insight(
                message_archive_id=int(row["message_archive_id"]),
                group_id=str(row["group_id"]),
                insight_type=INSIGHT_RECORD,
                segment_index=int(row["segment_index"] or 0),
                insight_key=str(row["insight_key"] or "0"),
                status=STATUS_FAILED,
                content=f"语音转写失败：{exc}",
                raw_result={"url": url, "name": name},
                error=str(exc),
            )
            failed_count += 1
            continue

        await upsert_insight(
            message_archive_id=int(row["message_archive_id"]),
            group_id=str(row["group_id"]),
            insight_type=INSIGHT_RECORD,
            segment_index=int(row["segment_index"] or 0),
            insight_key=str(row["insight_key"] or "0"),
            status=STATUS_READY,
            content=f"语音转写：{text}",
            raw_result={"url": url, "name": name, "text": text},
        )
        ready_count += 1

    return ready_count, failed_count, skipped_count


async def process_pending_links(limit: int | None = None, group_id: str | None = None) -> tuple[int, int, int]:
    await init_archive_db()
    process_limit = limit or batch_size()
    group_filter = "AND group_id = ?" if group_id else ""
    params: tuple[object, ...] = (
        INSIGHT_LINK,
        STATUS_PENDING,
        group_id,
        process_limit,
    ) if group_id else (
        INSIGHT_LINK,
        STATUS_PENDING,
        process_limit,
    )

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT id, message_archive_id, group_id, raw_result, segment_index, insight_key
            FROM message_insights
            WHERE insight_type = ? AND status = ?
            {group_filter}
            ORDER BY id ASC
            LIMIT ?
            """,
            params,
        )
        rows = await cursor.fetchall()

    ready_count = 0
    failed_count = 0
    skipped_count = 0
    for row in rows:
        payload = extract_link_payload(row["raw_result"])
        if payload is None:
            await upsert_insight(
                message_archive_id=int(row["message_archive_id"]),
                group_id=str(row["group_id"]),
                insight_type=INSIGHT_LINK,
                segment_index=int(row["segment_index"] or 0),
                insight_key=str(row["insight_key"] or "0"),
                status=STATUS_FAILED,
                content="链接解析失败：没有可用URL。",
                raw_result={"previous_raw_result": row["raw_result"]},
                error="missing url",
            )
            failed_count += 1
            continue

        try:
            analysis = await analyze_link_payload(payload)
        except Exception as exc:
            logger.exception("Link insight failed")
            await upsert_insight(
                message_archive_id=int(row["message_archive_id"]),
                group_id=str(row["group_id"]),
                insight_type=INSIGHT_LINK,
                segment_index=int(row["segment_index"] or 0),
                insight_key=str(row["insight_key"] or "0"),
                status=STATUS_FAILED,
                content=f"链接解析失败：{exc}",
                raw_result=payload,
                error=str(exc),
            )
            failed_count += 1
            continue

        await upsert_insight(
            message_archive_id=int(row["message_archive_id"]),
            group_id=str(row["group_id"]),
            insight_type=INSIGHT_LINK,
            segment_index=int(row["segment_index"] or 0),
            insight_key=str(row["insight_key"] or "0"),
            status=STATUS_READY,
            content=analysis,
            raw_result=payload,
        )
        ready_count += 1

    return ready_count, failed_count, skipped_count


def extract_forward_id(raw_result: str | None) -> str | None:
    if not raw_result:
        return None
    try:
        value = json.loads(raw_result)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    data = value.get("data")
    if not isinstance(data, dict):
        return None
    for key in ("id", "file", "file_id"):
        forward_id = data.get(key)
        if forward_id:
            return str(forward_id)
    return None


def normalize_forward_messages(result: object) -> list[dict[str, object]]:
    if isinstance(result, dict):
        for key in ("messages", "message", "data"):
            value = result.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = normalize_forward_messages(value)
                if nested:
                    return nested
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    return []


def message_value_to_text(value: object) -> str:
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        segments = [item for item in value if isinstance(item, dict)]
        if segments:
            return render_plain_text(segments)
    if isinstance(value, dict):
        return render_plain_text([value])
    return clean_text(str(value))


def forward_sender_text(node: dict[str, object]) -> str:
    sender = node.get("sender")
    if isinstance(sender, dict):
        for key in ("card", "nickname", "name", "user_id", "uin"):
            value = sender.get(key)
            if value:
                return str(value)
    for key in ("name", "nickname", "user_id", "uin"):
        value = node.get(key)
        if value:
            return str(value)
    return "未知发送者"


def forward_time_text(node: dict[str, object]) -> str:
    raw_time = node.get("time")
    if isinstance(raw_time, int | float):
        return datetime.fromtimestamp(raw_time).strftime("%Y-%m-%d %H:%M:%S")
    if raw_time:
        return str(raw_time)
    return ""


def forward_node_to_entry(
    *,
    index: int,
    forward_id: str,
    node: dict[str, object],
) -> dict[str, object]:
    text = message_value_to_text(node.get("message") or node.get("content"))
    if not text:
        text = "[空消息]"

    return {
        "source": "forward",
        "is_forward_message": True,
        "forward_id": forward_id,
        "index": index,
        "sender": forward_sender_text(node),
        "time": forward_time_text(node),
        "text": text,
        "raw_node": node,
    }


def build_forward_result_payload(
    *,
    forward_id: str,
    result: object,
    message_archive_id: int,
    group_id: str,
) -> dict[str, object]:
    messages = normalize_forward_messages(result)
    if not messages:
        raise ValueError("empty forward message")

    entries = [
        forward_node_to_entry(index=index, forward_id=forward_id, node=node)
        for index, node in enumerate(messages, start=1)
    ]
    return {
        "source": "forward",
        "insight_type": INSIGHT_FORWARD,
        "forward_id": forward_id,
        "message_archive_id": message_archive_id,
        "group_id": group_id,
        "message_count": len(entries),
        "messages": entries,
    }


def summarize_forward_payload(payload: dict[str, object]) -> str:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise ValueError("empty forward payload")

    lines = [
        "聊天记录展开",
        f"来源：合并聊天记录 forward_id={payload.get('forward_id', '')}",
        "说明：下面每一条都是聊天记录中的子消息，不是当前群直接发送的新消息。",
    ]
    for item in messages[:80]:
        if not isinstance(item, dict):
            continue
        index = item.get("index", "")
        sender = str(item.get("sender", "") or "未知发送者")
        sent_at = str(item.get("time", "") or "")
        text = str(item.get("text", "") or "[空消息]")
        prefix = f"[forward_message#{index}] 聊天记录消息#{index}. "
        if sent_at:
            prefix += f"[{sent_at}] "
        lines.append(f"{prefix}{sender}: {text}")
    if len(messages) > 80:
        lines.append(f"...聊天记录中还有 {len(messages) - 80} 条未展示")
    return truncate_text("\n".join(lines), 6000)


async def process_pending_forwards(
    bot: Bot,
    limit: int | None = None,
    group_id: str | None = None,
) -> tuple[int, int, int]:
    await init_archive_db()
    process_limit = limit or batch_size()
    group_filter = "AND group_id = ?" if group_id else ""
    params: tuple[object, ...] = (
        INSIGHT_FORWARD,
        STATUS_PENDING,
        group_id,
        process_limit,
    ) if group_id else (
        INSIGHT_FORWARD,
        STATUS_PENDING,
        process_limit,
    )

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT id, message_archive_id, group_id, raw_result, segment_index, insight_key
            FROM message_insights
            WHERE insight_type = ? AND status = ?
            {group_filter}
            ORDER BY id ASC
            LIMIT ?
            """,
            params,
        )
        rows = await cursor.fetchall()

    ready_count = 0
    failed_count = 0
    skipped_count = 0
    for row in rows:
        forward_id = extract_forward_id(row["raw_result"])
        if not forward_id:
            await upsert_insight(
                message_archive_id=int(row["message_archive_id"]),
                group_id=str(row["group_id"]),
                insight_type=INSIGHT_FORWARD,
                segment_index=int(row["segment_index"] or 0),
                insight_key=str(row["insight_key"] or "0"),
                status=STATUS_FAILED,
                content="聊天记录展开失败：没有可用的 forward id。",
                raw_result={"previous_raw_result": row["raw_result"]},
                error="missing forward id",
            )
            failed_count += 1
            continue

        try:
            result = await bot.call_api("get_forward_msg", id=forward_id)
            payload = build_forward_result_payload(
                forward_id=forward_id,
                result=result,
                message_archive_id=int(row["message_archive_id"]),
                group_id=str(row["group_id"]),
            )
            content = summarize_forward_payload(payload)
        except Exception as exc:
            logger.exception("Forward insight failed")
            await upsert_insight(
                message_archive_id=int(row["message_archive_id"]),
                group_id=str(row["group_id"]),
                insight_type=INSIGHT_FORWARD,
                segment_index=int(row["segment_index"] or 0),
                insight_key=str(row["insight_key"] or "0"),
                status=STATUS_FAILED,
                content=f"聊天记录展开失败：{exc}",
                raw_result={"forward_id": forward_id},
                error=str(exc),
            )
            failed_count += 1
            continue

        await upsert_insight(
            message_archive_id=int(row["message_archive_id"]),
            group_id=str(row["group_id"]),
            insight_type=INSIGHT_FORWARD,
            segment_index=int(row["segment_index"] or 0),
            insight_key=str(row["insight_key"] or "0"),
            status=STATUS_READY,
            content=content,
            raw_result=payload,
        )
        ready_count += 1

    return ready_count, failed_count, skipped_count


async def media_insight_status_text() -> str:
    await init_archive_db()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        total_cursor = await db.execute("SELECT COUNT(*) AS count FROM message_insights")
        total_row = await total_cursor.fetchone()
        by_status_cursor = await db.execute(
            """
            SELECT insight_type, status, COUNT(*) AS count
            FROM message_insights
            GROUP BY insight_type, status
            ORDER BY insight_type, status
            """
        )
        rows = await by_status_cursor.fetchall()

    lines = [
        "媒体识别状态",
        f"实验开关：{'开启' if media_insights_enabled() else '关闭'}",
        f"自动识别：{'开启' if auto_media_insights_enabled() else '关闭'}",
        f"图片调用：{'开启' if image_vision_enabled() else '关闭'}",
        f"图片模型：{image_vision_model()}",
        f"图片密钥：{'已配置' if image_vision_api_key() else '未配置'}",
        f"语音转写：{'开启' if voice_transcribe_enabled() else '关闭'}",
        f"语音模型：{voice_transcribe_model()}",
        f"识别记录：{int(total_row['count'] or 0) if total_row else 0}",
    ]
    for row in rows:
        lines.append(f"{row['insight_type']} / {row['status']}：{row['count']}")
    return "\n".join(lines)


@driver.on_startup
async def startup() -> None:
    if not media_insights_enabled():
        return
    await init_archive_db()
    try:
        await scan_recent_messages()
        if auto_media_insights_enabled():
            asyncio.create_task(process_pending_images())
            asyncio.create_task(process_pending_links())
            asyncio.create_task(process_pending_files())
            asyncio.create_task(process_pending_record_transcripts())
    except Exception:
        logger.exception("Media insight startup scan failed")


@auto_media_insight.handle()
async def handle_auto_media_insight(bot: Bot, event: MessageEvent) -> None:
    if not isinstance(event, GroupMessageEvent):
        return
    if not media_insights_enabled() or not auto_media_insights_enabled():
        return
    if not await is_group_feature_enabled(str(event.group_id), FEATURE_COLLECTOR):
        return

    segments = event_segments(event)
    if not has_insight_segments(segments):
        return

    message_id = str(getattr(event, "message_id", "") or "")
    if not message_id:
        return
    asyncio.create_task(auto_process_collected_message(bot, str(event.group_id), message_id))


@media_insight_status.handle()
async def handle_media_insight_status(event: Event) -> None:
    if denial := admin_denial(event):
        await media_insight_status.finish(Message(denial))
    await media_insight_status.finish(Message(await media_insight_status_text()))


@media_insight_scan.handle()
async def handle_media_insight_scan(bot: Bot, event: Event) -> None:
    if denial := admin_denial(event):
        await media_insight_scan.finish(Message(denial))
    if not media_insights_enabled():
        await media_insight_scan.finish(Message(f"媒体识别实验未开启，请设置 {MEDIA_INSIGHTS_ENABLED_ENV}=1。"))
    group_id = str(event.group_id) if isinstance(event, GroupMessageEvent) else None
    scanned_count, insight_count = await scan_recent_messages(group_id=group_id)
    visual_ready, visual_failed, visual_skipped = await process_pending_images(group_id=group_id)
    link_ready, link_failed, link_skipped = await process_pending_links(group_id=group_id)
    file_ready, file_failed, file_skipped = await process_pending_files(group_id=group_id)
    record_ready, record_failed, record_skipped = await process_pending_record_transcripts(group_id=group_id)
    forward_ready, forward_failed, forward_skipped = await process_pending_forwards(bot, group_id=group_id)
    lines = [
        f"已扫描最近{scanned_count}条消息，生成/更新{insight_count}条识别记录。",
        f"图片/表情包识别：成功{visual_ready}，失败{visual_failed}，跳过{visual_skipped}。",
        f"链接解析：成功{link_ready}，失败{link_failed}，跳过{link_skipped}。",
        f"文件读取：成功{file_ready}，失败{file_failed}，跳过{file_skipped}。",
        f"语音转写：成功{record_ready}，失败{record_failed}，跳过{record_skipped}。",
        f"聊天记录展开：成功{forward_ready}，失败{forward_failed}，跳过{forward_skipped}。",
    ]
    if visual_skipped:
        lines.append(f"跳过原因通常是未开启 {IMAGE_VISION_ENABLED_ENV}，或未配置 {IMAGE_VISION_API_KEY_ENV} / OPENAI_API_KEY。")
    await media_insight_scan.finish(Message("\n".join(lines)))
