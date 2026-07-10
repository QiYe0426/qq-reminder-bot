from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


@dataclass
class DailyReportVisualData:
    group_id: str
    group_name: str
    target_date: date
    total_messages: int = 0
    active_speakers: int = 0
    topic_count: int = 0
    keyword_count: int = 0
    insight_count: int = 0
    peak_hour: str = "无"
    top_speakers: list[tuple[str, int]] = field(default_factory=list)
    all_speakers: list[tuple[str, int]] = field(default_factory=list)
    highlights: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    generated_at: datetime = field(default_factory=datetime.now)


CANVAS_WIDTH = 1080
BASE_CANVAS_HEIGHT = 5400
MARGIN_X = 80

GREEN_50 = "#F7FCEB"
GREEN_100 = "#EEF8D7"
GREEN_200 = "#D9EFB4"
GREEN_300 = "#C2E58A"
GREEN_500 = "#78A64F"
GREEN_700 = "#345F3E"
TEXT_DARK = "#2D3B28"
TEXT_MUTED = "#6F7F62"
CARD_BG = "#FFFFF8"
CARD_BORDER = "#D4EAA5"
SHADOW = "#DCEBBE"
GOLD = "#F0C955"


def _font_candidates(bold: bool = False) -> list[str]:
    env_font = os.getenv("DAILY_REPORT_VISUAL_FONT")
    candidates = [env_font] if env_font else []
    candidates.extend(
        [
            "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "C:/Windows/Fonts/simsun.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    )
    return [candidate for candidate in candidates if candidate]


def load_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in _font_candidates(bold=bold):
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def row_get(row: Any, key: str, default: Any = "") -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return getattr(row, key, default)


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def strip_markdown(text: str) -> str:
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
    text = re.sub(r"\[[^\]]+\]\([^)]+\)", "", text)
    return normalize_text(text.strip(" -\t#>*"))


def speaker_name(row: Any) -> str:
    name = normalize_text(row_get(row, "sender_name"))
    if name:
        return name
    if str(row_get(row, "sub_type")) == "ai_reply":
        return "猎宝"
    return normalize_text(row_get(row, "user_id", "未知群友")) or "未知群友"


def created_hour(row: Any) -> str | None:
    raw = normalize_text(row_get(row, "created_at"))
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw[:19], fmt).strftime("%H:00")
        except ValueError:
            continue
    match = re.search(r"\b(\d{1,2}):\d{2}", raw)
    if match:
        return f"{int(match.group(1)):02d}:00"
    return None


def parse_segment_types(row: Any) -> list[str]:
    raw = row_get(row, "segment_types")
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


def extract_markdown_section(markdown: str, title: str) -> str:
    lines = markdown.splitlines()
    collecting = False
    section_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            heading = strip_markdown(stripped.removeprefix("## "))
            if collecting:
                break
            collecting = heading == title
            continue
        if collecting:
            section_lines.append(line)
    return "\n".join(section_lines).strip()


def section_items(section: str, *, limit: int, max_chars: int = 54) -> list[str]:
    items: list[str] = []
    for raw_line in section.splitlines():
        line = strip_markdown(raw_line)
        if not line or line.startswith("暂无"):
            continue
        line = re.sub(r"^\d+[.)、]\s*", "", line)
        if len(line) > max_chars:
            line = line[: max_chars - 1].rstrip() + "…"
        if line not in items:
            items.append(line)
        if len(items) >= limit:
            break
    return items


def extract_topic_titles(markdown: str, *, limit: int = 4) -> list[str]:
    section = extract_markdown_section(markdown, "分话题总结")
    topics: list[str] = []
    for raw_line in section.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("### "):
            title = strip_markdown(stripped.removeprefix("### "))
        elif stripped.startswith(("- ", "* ")):
            title = strip_markdown(stripped[2:])
        else:
            continue
        title = re.split(r"[：:，,。；;]", title, maxsplit=1)[0].strip()
        if not title:
            continue
        if len(title) > 14:
            title = title[:13] + "…"
        if title not in topics:
            topics.append(title)
        if len(topics) >= limit:
            break
    return topics


def extract_keywords_from_stats(markdown: str, *, limit: int = 8) -> list[str]:
    stats = extract_markdown_section(markdown, "统计信息")
    match = re.search(r"初步话题线索[：:]\s*(.+)", stats)
    if not match:
        return []
    keywords: list[str] = []
    for part in re.split(r"[,，、]\s*", match.group(1)):
        item = re.split(r"[:：]\d+", part.strip(), maxsplit=1)[0].strip()
        if item and item != "无" and item not in keywords:
            keywords.append(item)
        if len(keywords) >= limit:
            break
    return keywords


def keyword_candidates(rows: list[Any], markdown: str, *, limit: int = 8) -> list[str]:
    from_stats = extract_keywords_from_stats(markdown, limit=limit)
    if from_stats:
        return from_stats

    counter: Counter[str] = Counter()
    stop_words = {"这个", "那个", "今天", "昨天", "然后", "但是", "就是", "没有", "可以", "什么", "一下"}
    for row in rows:
        text = normalize_text(row_get(row, "plain_text"))
        for token in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,12}", text):
            if token in stop_words or token.isdigit():
                continue
            counter[token] += 1
    return [key for key, _value in counter.most_common(limit)]


def fallback_highlights(rows: list[Any], topics: list[str], keywords: list[str]) -> list[str]:
    if topics:
        return [f"{topic} 成为今日主要讨论点" for topic in topics[:3]]
    if keywords:
        return [f"高频关键词：{keyword}" for keyword in keywords[:3]]
    if rows:
        return ["今天有新的群聊记录入库", "可以在 Markdown 附录里回看完整时间线"]
    return ["当天没有采集到消息，日报保持空白留档"]


def build_visual_data(
    *,
    group_id: str,
    target_date: date,
    group_name: str | None,
    markdown_content: str,
    rows: list[Any],
    insights_by_message_id: dict[int, list[dict[str, object]]],
) -> DailyReportVisualData:
    speaker_counts: Counter[str] = Counter()
    hour_counts: Counter[str] = Counter()
    insight_count = 0
    for row in rows:
        speaker_counts[speaker_name(row)] += 1
        hour = created_hour(row)
        if hour:
            hour_counts[hour] += 1
        try:
            row_id = int(row_get(row, "id"))
        except Exception:
            row_id = 0
        insight_count += len(insights_by_message_id.get(row_id, []))

    topics = extract_topic_titles(markdown_content, limit=4)
    keywords = keyword_candidates(rows, markdown_content, limit=8)
    highlights = section_items(extract_markdown_section(markdown_content, "今日重点"), limit=3, max_chars=42)
    if not highlights:
        highlights = fallback_highlights(rows, topics, keywords)
    if not topics:
        topics = [item[:14] for item in highlights[:4]]
    if not keywords:
        keywords = topics[:]

    peak_hour = "无"
    if hour_counts:
        hour, count = hour_counts.most_common(1)[0]
        peak_hour = f"{hour}（{count}条）"

    return DailyReportVisualData(
        group_id=group_id,
        group_name=group_name or f"群{group_id}",
        target_date=target_date,
        total_messages=len(rows),
        active_speakers=len(speaker_counts),
        topic_count=max(len(topics), len(highlights)),
        keyword_count=len(keywords),
        insight_count=insight_count,
        peak_hour=peak_hour,
        top_speakers=speaker_counts.most_common(3),
        all_speakers=speaker_counts.most_common(),
        highlights=highlights[:3],
        topics=topics[:4],
        keywords=keywords[:8],
    )


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]


def _blend(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(a[index] + (b[index] - a[index]) * t) for index in range(3))  # type: ignore[return-value]


def make_gradient_background(width: int, height: int) -> Image.Image:
    top = _hex_to_rgb("#E3F7B8")
    middle = _hex_to_rgb("#FBFFF4")
    bottom = _hex_to_rgb("#F0F9D8")
    image = Image.new("RGB", (width, height), GREEN_50)
    pixels = image.load()
    for y in range(height):
        t = y / max(height - 1, 1)
        if t < 0.45:
            color = _blend(top, middle, t / 0.45)
        else:
            color = _blend(middle, bottom, (t - 0.45) / 0.55)
        for x in range(width):
            pixels[x, y] = color
    return image


def text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    *,
    max_lines: int | None = None,
) -> list[str]:
    lines: list[str] = []
    current = ""
    for char in text:
        if char == "\n":
            lines.append(current)
            current = ""
            continue
        candidate = current + char
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = char
        if max_lines and len(lines) >= max_lines:
            break
    if (not max_lines or len(lines) < max_lines) and current:
        lines.append(current)
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
    if max_lines and len(lines) == max_lines and draw.textlength(lines[-1], font=font) > max_width - 24:
        while lines[-1] and draw.textlength(lines[-1] + "…", font=font) > max_width:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    return lines or [""]


def draw_centered_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: str,
    *,
    width: int = CANVAS_WIDTH,
) -> None:
    text_width, _height = text_size(draw, text, font)
    draw.text(((width - text_width) / 2, xy[1]), text, font=font, fill=fill)


def rounded_card(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    *,
    radius: int = 36,
    fill: str = CARD_BG,
    outline: str = CARD_BORDER,
    shadow: bool = True,
) -> None:
    x0, y0, x1, y1 = box
    if shadow:
        draw.rounded_rectangle((x0 + 8, y0 + 10, x1 + 8, y1 + 10), radius=radius, fill=SHADOW)
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=4)


def draw_star(draw: ImageDraw.ImageDraw, center: tuple[int, int], radius: int, fill: str = "#FFFFFF") -> None:
    cx, cy = center
    points: list[tuple[float, float]] = []
    for idx in range(8):
        angle = math.pi / 4 * idx
        r = radius if idx % 2 == 0 else radius * 0.35
        points.append((cx + math.cos(angle) * r, cy + math.sin(angle) * r))
    draw.polygon(points, fill=fill)


def draw_leaf(draw: ImageDraw.ImageDraw, center: tuple[int, int], size: int, angle: float = 0.0) -> None:
    cx, cy = center
    for idx in range(5):
        offset = (idx - 2) * size * 0.22
        x = cx + int(math.cos(angle) * offset)
        y = cy + int(math.sin(angle) * offset)
        draw.ellipse((x - size // 4, y - size // 2, x + size // 4, y + size // 2), fill="#A9CF6A", outline=GREEN_700, width=2)
    draw.line((cx - size, cy + size, cx + size, cy - size), fill=GREEN_700, width=3)


def draw_mascot(draw: ImageDraw.ImageDraw, x: int, y: int, scale: float = 1.0) -> None:
    s = scale
    hood = "#376D45"
    face = "#F8F4D8"
    hair = "#F5F7F0"
    dark = "#26352A"
    draw.ellipse((x, y + int(36 * s), x + int(210 * s), y + int(260 * s)), fill=hood, outline=dark, width=max(2, int(3 * s)))
    draw.polygon(
        [
            (x + int(36 * s), y + int(62 * s)),
            (x + int(82 * s), y),
            (x + int(150 * s), y + int(46 * s)),
            (x + int(120 * s), y + int(80 * s)),
        ],
        fill="#EFE9C9",
        outline=dark,
    )
    draw.ellipse((x + int(54 * s), y + int(84 * s), x + int(160 * s), y + int(190 * s)), fill=face, outline=dark, width=max(2, int(3 * s)))
    draw.polygon(
        [(x + int(52 * s), y + int(95 * s)), (x + int(92 * s), y + int(74 * s)), (x + int(82 * s), y + int(132 * s))],
        fill=hair,
    )
    draw.polygon(
        [(x + int(130 * s), y + int(78 * s)), (x + int(168 * s), y + int(100 * s)), (x + int(138 * s), y + int(136 * s))],
        fill=hair,
    )
    draw.ellipse((x + int(78 * s), y + int(126 * s), x + int(96 * s), y + int(154 * s)), fill="#7EBA6A", outline=dark)
    draw.ellipse((x + int(124 * s), y + int(126 * s), x + int(142 * s), y + int(154 * s)), fill="#7EBA6A", outline=dark)
    draw.arc((x + int(91 * s), y + int(152 * s), x + int(132 * s), y + int(178 * s)), start=10, end=170, fill=dark, width=max(2, int(3 * s)))
    draw.line((x + int(34 * s), y + int(198 * s), x + int(8 * s), y + int(238 * s)), fill=dark, width=max(4, int(5 * s)))
    draw.line((x + int(178 * s), y + int(198 * s), x + int(216 * s), y + int(228 * s)), fill=dark, width=max(4, int(5 * s)))
    draw.rounded_rectangle((x + int(70 * s), y + int(204 * s), x + int(152 * s), y + int(298 * s)), radius=int(26 * s), fill=hood, outline=dark, width=max(2, int(3 * s)))


def draw_section_pill(draw: ImageDraw.ImageDraw, y: int, title: str, font: ImageFont.ImageFont) -> int:
    pill_width = 330
    pill_height = 76
    x0 = (CANVAS_WIDTH - pill_width) // 2
    draw.rounded_rectangle((x0, y, x0 + pill_width, y + pill_height), radius=38, fill=GREEN_200)
    draw.ellipse((x0 + 45, y + 31, x0 + 56, y + 42), fill="#FFFFFF")
    draw.ellipse((x0 + pill_width - 56, y + 31, x0 + pill_width - 45, y + 42), fill="#FFFFFF")
    draw_centered_text(draw, (0, y + 17), title, font, TEXT_DARK)
    return y + pill_height


def draw_overview(draw: ImageDraw.ImageDraw, y: int, data: DailyReportVisualData, fonts: dict[str, ImageFont.ImageFont]) -> int:
    card_x0 = MARGIN_X
    card_x1 = CANVAS_WIDTH - MARGIN_X
    rounded_card(draw, (card_x0, y, card_x1, y + 360), radius=40)

    metrics = [
        ("总发言量", str(data.total_messages)),
        ("活跃人数", str(data.active_speakers)),
        ("新增话题数", str(data.topic_count)),
        ("高频关键词数", str(data.keyword_count)),
        ("素材识别数", str(data.insight_count)),
        ("高峰时段", data.peak_hour),
    ]
    columns = 3
    cell_width = (card_x1 - card_x0 - 60) // columns
    for index, (label, value) in enumerate(metrics):
        row = index // columns
        col = index % columns
        x = card_x0 + 36 + col * cell_width
        yy = y + 44 + row * 118
        draw.text((x, yy), label, font=fonts["small"], fill=GREEN_700)
        value_lines = wrap_text(draw, value, fonts["metric"], cell_width - 24, max_lines=1)
        draw.text((x, yy + 34), value_lines[0], font=fonts["metric"], fill=TEXT_DARK)
        draw.line((x, yy + 88, x + cell_width - 34, yy + 88), fill=GREEN_200, width=3)

    speaker_y = y + 280
    if data.top_speakers:
        draw.text((card_x0 + 36, speaker_y), "活跃群友", font=fonts["small"], fill=TEXT_MUTED)
        badge_x = card_x0 + 156
        for name, count in data.top_speakers:
            label = f"{name} {count}"
            label_width = int(draw.textlength(label, font=fonts["tiny"])) + 34
            draw.rounded_rectangle((badge_x, speaker_y - 4, badge_x + label_width, speaker_y + 34), radius=19, fill=GREEN_100, outline=GREEN_200, width=2)
            draw.text((badge_x + 17, speaker_y + 3), label, font=fonts["tiny"], fill=TEXT_DARK)
            badge_x += label_width + 14
            if badge_x > card_x1 - 120:
                break

    return y + 420


def draw_speaker_breakdown(draw: ImageDraw.ImageDraw, y: int, data: DailyReportVisualData, fonts: dict[str, ImageFont.ImageFont]) -> int:
    speakers = data.all_speakers or [("暂无", 0)]
    max_count = max(c for _, c in speakers) if speakers else 1
    rows_needed = (len(speakers) + 1) // 2
    row_h = 54
    header_h = 50
    padding_y = 36
    card_h = padding_y * 2 + header_h + rows_needed * row_h + 20
    card_x0 = MARGIN_X
    card_x1 = CANVAS_WIDTH - MARGIN_X

    rounded_card(draw, (card_x0, y, card_x1, y + card_h), radius=40, fill="#FFFFFF", outline="#E7F3C3")

    col_widths = [22, 180, 80, 70, 22, 180, 80]
    col_starts: list[int] = []
    left = card_x0 + 36
    for cw in col_widths:
        col_starts.append(left)
        left += cw

    draw.text((col_starts[0], y + padding_y), "#", font=fonts["tiny"], fill=TEXT_MUTED)
    draw.text((col_starts[1], y + padding_y), "群友", font=fonts["tiny"], fill=TEXT_MUTED)
    draw.text((col_starts[2], y + padding_y), "消息", font=fonts["tiny"], fill=TEXT_MUTED)
    draw.text((col_starts[4], y + padding_y), "#", font=fonts["tiny"], fill=TEXT_MUTED)
    draw.text((col_starts[5], y + padding_y), "群友", font=fonts["tiny"], fill=TEXT_MUTED)
    draw.text((col_starts[6], y + padding_y), "消息", font=fonts["tiny"], fill=TEXT_MUTED)
    sep_y = y + padding_y + header_h
    draw.line((card_x0 + 36, sep_y, card_x1 - 36, sep_y), fill=GREEN_200, width=2)

    bar_max_w = 100
    for idx, (name, count) in enumerate(speakers):
        col_idx = idx % 2
        row_idx = idx // 2
        xx = (col_starts[0] if col_idx == 0 else col_starts[4])
        nx = (col_starts[1] if col_idx == 0 else col_starts[5])
        cx = (col_starts[2] if col_idx == 0 else col_starts[6])
        ny = sep_y + 12 + row_idx * row_h

        draw.text((xx, ny), str(idx + 1), font=fonts["tiny"], fill=TEXT_DARK)
        name_display = name if len(name) <= 10 else name[:9] + "…"
        draw.text((nx, ny), name_display, font=fonts["tiny"], fill=TEXT_DARK)
        draw.text((cx, ny), str(count), font=fonts["tiny"], fill=TEXT_MUTED)

        bar_w = int(bar_max_w * count / max_count) if max_count else 0
        if bar_w > 0:
            bar_x = cx + 36
            draw.rounded_rectangle((bar_x, ny + 6, bar_x + bar_w, ny + 18), radius=6, fill=GREEN_200)

    return y + card_h + 20


def draw_highlights(draw: ImageDraw.ImageDraw, y: int, data: DailyReportVisualData, fonts: dict[str, ImageFont.ImageFont]) -> int:
    rounded_card(draw, (MARGIN_X, y, CANVAS_WIDTH - MARGIN_X, y + 470), radius=42, fill="#FBFFF0", outline="#E7F3C3")
    draw_mascot(draw, CANVAS_WIDTH - 345, y + 160, scale=0.82)
    note_colors = ["#FFFFF1", "#F9FFE9", "#FFFCEF"]
    for index, item in enumerate(data.highlights[:3]):
        x = MARGIN_X + 70 + (index % 2) * 44
        yy = y + 68 + index * 112
        draw.rounded_rectangle((x, yy, x + 430, yy + 76), radius=12, fill=note_colors[index % len(note_colors)], outline=GREEN_200, width=3)
        draw.line((x + 28, yy - 10, x + 116, yy + 2), fill=GOLD, width=5)
        lines = wrap_text(draw, item, fonts["body"], 378, max_lines=2)
        for line_index, line in enumerate(lines):
            draw.text((x + 28, yy + 18 + line_index * 27), line, font=fonts["body"], fill=TEXT_DARK)
    return y + 530


def draw_topics(draw: ImageDraw.ImageDraw, y: int, data: DailyReportVisualData, fonts: dict[str, ImageFont.ImageFont]) -> int:
    rounded_card(draw, (MARGIN_X, y, CANVAS_WIDTH - MARGIN_X, y + 530), radius=42, fill="#FBFFF0", outline="#E7F3C3")
    card_w = 395
    card_h = 135
    start_x = MARGIN_X + 70
    start_y = y + 70
    topics = data.topics[:4] or ["暂无明显话题"]
    for index in range(4):
        topic = topics[index] if index < len(topics) else "待补充"
        col = index % 2
        row = index // 2
        x = start_x + col * (card_w + 70)
        yy = start_y + row * 185
        draw.rounded_rectangle((x + 6, yy + 8, x + card_w + 6, yy + card_h + 8), radius=24, fill=SHADOW)
        draw.rounded_rectangle((x, yy, x + card_w, yy + card_h), radius=24, fill="#FFFFFF", outline=GREEN_200, width=3)
        draw.ellipse((x + 24, yy + 30, x + 58, yy + 64), fill=GREEN_200)
        lines = wrap_text(draw, topic, fonts["topic"], card_w - 108, max_lines=2)
        for line_index, line in enumerate(lines):
            draw.text((x + 78, yy + 38 + line_index * 34), line, font=fonts["topic"], fill=TEXT_DARK)
        if index in {1, 3}:
            draw_leaf(draw, (x + card_w - 36, yy + card_h - 34), 38, angle=-0.8)
    return y + 590


def draw_semantic_graph(draw: ImageDraw.ImageDraw, y: int, data: DailyReportVisualData, fonts: dict[str, ImageFont.ImageFont]) -> int:
    rounded_card(draw, (MARGIN_X, y, CANVAS_WIDTH - MARGIN_X, y + 560), radius=42, fill="#FFFFFF", outline="#E7F3C3")
    draw.text((MARGIN_X + 46, y + 42), "话题语义图", font=fonts["small"], fill=GREEN_500)
    graph_box = (MARGIN_X + 60, y + 110, CANVAS_WIDTH - MARGIN_X - 350, y + 470)
    nodes = (data.topics + data.keywords)[:10] or ["暂无话题"]
    positions = [
        (0.12, 0.18),
        (0.46, 0.12),
        (0.78, 0.22),
        (0.30, 0.42),
        (0.58, 0.48),
        (0.18, 0.68),
        (0.46, 0.78),
        (0.80, 0.70),
        (0.08, 0.48),
        (0.66, 0.35),
    ]
    node_points: list[tuple[int, int]] = []
    x0, y0, x1, y1 = graph_box
    for idx, _node in enumerate(nodes):
        px = x0 + int((x1 - x0) * positions[idx][0])
        py = y0 + int((y1 - y0) * positions[idx][1])
        node_points.append((px, py))
    for idx, start in enumerate(node_points):
        for end_index in (idx + 1, idx + 3):
            if end_index < len(node_points):
                draw.line((start[0], start[1], node_points[end_index][0], node_points[end_index][1]), fill="#BBD58A", width=3)
    for idx, (node, (px, py)) in enumerate(zip(nodes, node_points)):
        radius = 25 if idx < len(data.topics) else 18
        draw.ellipse((px - radius, py - radius, px + radius, py + radius), fill=GREEN_200 if idx < len(data.topics) else "#D8EAA8", outline=GREEN_700, width=2)
        label = node if len(node) <= 6 else node[:5] + "…"
        label_w, _ = text_size(draw, label, fonts["tiny"])
        draw.text((px - label_w / 2, py + radius + 8), label, font=fonts["tiny"], fill=TEXT_MUTED)
    draw_mascot(draw, CANVAS_WIDTH - MARGIN_X - 288, y + 185, scale=0.88)
    return y + 620


def render_daily_report_image(data: DailyReportVisualData, image_path: Path, *, width: int = CANVAS_WIDTH) -> Path:
    image = make_gradient_background(width, BASE_CANVAS_HEIGHT)
    draw = ImageDraw.Draw(image)
    fonts = {
        "title": load_font(76, bold=True),
        "subtitle": load_font(34, bold=True),
        "section": load_font(42, bold=True),
        "metric": load_font(34, bold=True),
        "topic": load_font(31, bold=True),
        "body": load_font(27),
        "small": load_font(24, bold=True),
        "tiny": load_font(20),
        "footer": load_font(18),
    }

    draw_leaf(draw, (55, 290), 70, angle=-0.5)
    draw_leaf(draw, (1006, 270), 74, angle=0.55)
    draw_star(draw, (170, 122), 18, fill="#FFFFFF")
    draw_star(draw, (912, 82), 28, fill="#FFFFFF")
    draw_star(draw, (840, 278), 24, fill=GOLD)

    title = "猎宝日报"
    title_width, _ = text_size(draw, title, fonts["title"])
    title_x = (width - title_width) / 2
    draw.text((title_x + 4, 116 + 6), title, font=fonts["title"], fill="#9CB879")
    draw.text((title_x, 116), title, font=fonts["title"], fill="#FFFFFF", stroke_width=2, stroke_fill="#D4EAA5")

    date_text = data.target_date.strftime("%-m月%-d日") if os.name != "nt" else f"{data.target_date.month}月{data.target_date.day}日"
    subtitle = f"{date_text} | {data.group_name}"
    draw_centered_text(draw, (0, 226), subtitle, fonts["subtitle"], TEXT_DARK)
    draw_mascot(draw, 270, 292, scale=1.25)
    draw.arc((620, 210, 940, 430), start=190, end=345, fill="#FFFFFF", width=3)
    draw.ellipse((830, 300, 890, 352), fill="#E9D14B", outline=TEXT_DARK, width=3)
    draw.line((840, 350, 820, 384), fill=TEXT_DARK, width=3)
    draw.line((874, 348, 904, 378), fill=TEXT_DARK, width=3)

    y = 690
    y = draw_section_pill(draw, y, "概览", fonts["section"]) + 54
    y = draw_overview(draw, y, data, fonts)
    y = draw_section_pill(draw, y, "活跃发言人", fonts["section"]) + 54
    y = draw_speaker_breakdown(draw, y, data, fonts)
    y = draw_section_pill(draw, y, "今日重点", fonts["section"]) + 54
    y = draw_highlights(draw, y, data, fonts)
    y = draw_section_pill(draw, y, "分话题总结", fonts["section"]) + 54
    y = draw_topics(draw, y, data, fonts)
    y = draw_section_pill(draw, y, "话题语义图", fonts["section"]) + 54
    y = draw_semantic_graph(draw, y, data, fonts)

    footer = f"生成时间：{data.generated_at.strftime('%Y-%m-%d %H:%M')}  |  群号：{data.group_id}"
    draw_centered_text(draw, (0, y + 10), footer, fonts["footer"], TEXT_MUTED)
    y += 90

    image = image.crop((0, 0, width, min(y, BASE_CANVAS_HEIGHT)))
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(image_path, format="PNG", optimize=True)
    return image_path


def write_daily_report_pdf_from_image(image_path: Path, pdf_path: Path) -> Path:
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    with Image.open(image_path) as image:
        width_px, height_px = image.size

    scale = 0.72
    page_size = (width_px * scale, height_px * scale)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(pdf_path), pagesize=page_size)
    c.setTitle(pdf_path.stem)
    c.drawImage(ImageReader(str(image_path)), 0, 0, width=page_size[0], height=page_size[1], preserveAspectRatio=False, mask="auto")
    c.showPage()
    c.save()
    return pdf_path
