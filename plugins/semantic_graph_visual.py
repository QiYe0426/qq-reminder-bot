from __future__ import annotations

import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from plugins.daily_report_visual import load_font


GRAPH_DIR = Path("data/semantic_graphs")
CANVAS_WIDTH = 1280
CANVAS_HEIGHT = 1500
GREEN_50 = "#F7FCEB"
GREEN_100 = "#EEF8D7"
GREEN_200 = "#D9EFB4"
GREEN_300 = "#BFE48A"
GREEN_500 = "#78A64F"
GREEN_700 = "#31583B"
TEXT_DARK = "#24351F"
TEXT_MUTED = "#6F7F62"
CARD_BG = "#FFFFF8"
CARD_BORDER = "#D4EAA5"
GOLD = "#F0C955"
BLUE = "#78A8D8"
PINK = "#E8A7B7"


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def safe_filename_part(value: str, fallback: str = "semantic-graph") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n]+', "_", normalize_text(value))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    return cleaned[:80] or fallback


def semantic_graph_image_filename(graph: dict[str, object]) -> str:
    group_id = normalize_text(graph.get("group_id")) or "group"
    graph_id = normalize_text(graph.get("graph_id")) or datetime.now().strftime("%Y%m%d%H%M%S")
    source_key = normalize_text(graph.get("source_key")) or "recent"
    title_part = safe_filename_part(f"{source_key}_{graph_id.replace(':', '-')}")
    return f"semantic-graph-{group_id}-{title_part}.png"


def semantic_graph_image_path(filename: str) -> Path:
    GRAPH_DIR.mkdir(parents=True, exist_ok=True)
    return (GRAPH_DIR / Path(filename).name).resolve()


def text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def draw_centered_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font,
    fill: str,
) -> None:
    width, height = text_size(draw, text, font)
    draw.text((xy[0] - width / 2, xy[1] - height / 2), text, font=font, fill=fill)


def node_color(kind: str) -> str:
    if kind == "person":
        return BLUE
    if kind == "topic":
        return GREEN_300
    return PINK


def edge_color(relation: str) -> str:
    if relation == "提到":
        return "#9EC485"
    if relation == "接续讨论":
        return "#9FB9D8"
    return "#D9C96B"


def wrap_label(label: str, *, limit: int = 8) -> str:
    label = normalize_text(label)
    if len(label) <= limit:
        return label
    return label[: limit - 1] + "…"


def graph_nodes(graph: dict[str, object], *, limit: int = 32) -> list[dict[str, object]]:
    nodes = [item for item in graph.get("nodes", []) if isinstance(item, dict)]
    nodes.sort(key=lambda item: float(item.get("weight") or 1), reverse=True)
    return nodes[:limit]


def graph_edges(graph: dict[str, object], node_labels: set[tuple[str, str]], *, limit: int = 70) -> list[dict[str, object]]:
    edges = []
    for item in graph.get("edges", []):
        if not isinstance(item, dict):
            continue
        source = (normalize_text(item.get("source")), normalize_text(item.get("source_kind") or "topic"))
        target = (normalize_text(item.get("target")), normalize_text(item.get("target_kind") or "topic"))
        if source in node_labels and target in node_labels:
            edges.append(item)
    edges.sort(key=lambda item: float(item.get("weight") or 1), reverse=True)
    return edges[:limit]


def layout_nodes(nodes: list[dict[str, object]]) -> dict[tuple[str, str], tuple[float, float]]:
    center_x = CANVAS_WIDTH / 2
    center_y = 680
    person_nodes = [node for node in nodes if node.get("kind") == "person"]
    topic_nodes = [node for node in nodes if node.get("kind") != "person"]
    positions: dict[tuple[str, str], tuple[float, float]] = {}

    def put_ring(items: list[dict[str, object]], radius_x: float, radius_y: float, angle_offset: float) -> None:
        count = max(1, len(items))
        for index, node in enumerate(items):
            angle = angle_offset + 2 * math.pi * index / count
            label = normalize_text(node.get("label"))
            kind = normalize_text(node.get("kind") or "topic")
            positions[(label, kind)] = (
                center_x + math.cos(angle) * radius_x,
                center_y + math.sin(angle) * radius_y,
            )

    put_ring(topic_nodes[:20], 430, 310, -math.pi / 2)
    put_ring(person_nodes[:12], 255, 190, -math.pi / 2 + 0.3)
    return positions


def draw_header(draw: ImageDraw.ImageDraw, graph: dict[str, object]) -> None:
    title_font = load_font(58, bold=True)
    sub_font = load_font(26)
    chip_font = load_font(24, bold=True)
    title = "猎宝语义图"
    graph_title = normalize_text(graph.get("title")) or "群聊语义图"
    draw_centered_text(draw, (CANVAS_WIDTH / 2, 86), title, title_font, TEXT_DARK)
    draw_centered_text(draw, (CANVAS_WIDTH / 2, 145), graph_title[:42], sub_font, TEXT_MUTED)
    stats = [
        f"消息 {int(graph.get('message_count') or 0)}",
        f"节点 {int(graph.get('node_count') or 0)}",
        f"关系 {int(graph.get('edge_count') or 0)}",
    ]
    x = 370
    for stat in stats:
        width, _height = text_size(draw, stat, chip_font)
        draw.rounded_rectangle((x, 185, x + width + 42, 235), radius=25, fill=GREEN_100, outline=CARD_BORDER, width=2)
        draw.text((x + 21, 196), stat, font=chip_font, fill=GREEN_700)
        x += width + 62


def draw_legend(draw: ImageDraw.ImageDraw) -> None:
    font = load_font(24)
    items = [("发言人", BLUE), ("话题/关键词", GREEN_300), ("关系强度", GOLD)]
    x = 92
    y = 1200
    for label, color in items:
        draw.ellipse((x, y, x + 22, y + 22), fill=color, outline=GREEN_700)
        draw.text((x + 34, y - 3), label, font=font, fill=TEXT_MUTED)
        x += 170


def draw_edges(
    draw: ImageDraw.ImageDraw,
    edges: list[dict[str, object]],
    positions: dict[tuple[str, str], tuple[float, float]],
) -> None:
    for edge in reversed(edges):
        source = (normalize_text(edge.get("source")), normalize_text(edge.get("source_kind") or "topic"))
        target = (normalize_text(edge.get("target")), normalize_text(edge.get("target_kind") or "topic"))
        if source not in positions or target not in positions:
            continue
        weight = max(1.0, float(edge.get("weight") or 1))
        width = max(2, min(10, int(1 + math.sqrt(weight) * 2)))
        color = edge_color(normalize_text(edge.get("relation")))
        draw.line((*positions[source], *positions[target]), fill=color, width=width)


def draw_nodes(
    draw: ImageDraw.ImageDraw,
    nodes: list[dict[str, object]],
    positions: dict[tuple[str, str], tuple[float, float]],
) -> None:
    label_font = load_font(22, bold=True)
    small_font = load_font(18)
    max_weight = max([float(node.get("weight") or 1) for node in nodes] or [1.0])
    for node in sorted(nodes, key=lambda item: float(item.get("weight") or 1)):
        label = normalize_text(node.get("label"))
        kind = normalize_text(node.get("kind") or "topic")
        pos = positions.get((label, kind))
        if not pos:
            continue
        weight = max(1.0, float(node.get("weight") or 1))
        radius = 34 + int(30 * math.sqrt(weight / max_weight))
        fill = node_color(kind)
        x, y = pos
        draw.ellipse((x - radius + 4, y - radius + 5, x + radius + 4, y + radius + 5), fill="#DDECC0")
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill, outline=GREEN_700, width=3)
        draw_centered_text(draw, (x, y - 5), wrap_label(label), label_font, TEXT_DARK)
        draw_centered_text(draw, (x, y + 24), str(int(round(weight))), small_font, TEXT_MUTED)


def draw_top_relations(draw: ImageDraw.ImageDraw, edges: list[dict[str, object]]) -> None:
    title_font = load_font(30, bold=True)
    font = load_font(22)
    x0, y0, x1, y1 = 80, 1260, CANVAS_WIDTH - 80, 1430
    draw.rounded_rectangle((x0 + 6, y0 + 8, x1 + 6, y1 + 8), radius=32, fill="#E4F0C9")
    draw.rounded_rectangle((x0, y0, x1, y1), radius=32, fill=CARD_BG, outline=CARD_BORDER, width=3)
    draw.text((x0 + 32, y0 + 24), "主要关系", font=title_font, fill=GREEN_700)
    if not edges:
        draw.text((x0 + 32, y0 + 75), "暂无关系。请确认所选范围内有足够消息。", font=font, fill=TEXT_MUTED)
        return
    for index, edge in enumerate(edges[:4]):
        line = (
            f"{edge.get('source')} --{edge.get('relation')}→ {edge.get('target')}"
            f"（{float(edge.get('weight') or 1):.0f}）"
        )
        draw.text((x0 + 32, y0 + 72 + index * 30), line[:58], font=font, fill=TEXT_DARK)


def render_semantic_graph_image(graph: dict[str, object], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), GREEN_50)
    draw = ImageDraw.Draw(image)

    draw_header(draw, graph)
    draw.rounded_rectangle((58, 270, CANVAS_WIDTH - 58, 1168), radius=42, fill=CARD_BG, outline=CARD_BORDER, width=4)
    draw_legend(draw)

    nodes = graph_nodes(graph)
    labels = {(normalize_text(node.get("label")), normalize_text(node.get("kind") or "topic")) for node in nodes}
    edges = graph_edges(graph, labels)
    positions = layout_nodes(nodes)
    draw_edges(draw, edges, positions)
    draw_nodes(draw, nodes, positions)
    if not nodes:
        empty_font = load_font(34, bold=True)
        draw_centered_text(draw, (CANVAS_WIDTH / 2, 700), "暂无可视化节点", empty_font, TEXT_MUTED)
    draw_top_relations(draw, edges)
    image.save(output_path)


def render_semantic_graph_to_file(graph: dict[str, object]) -> tuple[str, Path]:
    filename = semantic_graph_image_filename(graph)
    path = semantic_graph_image_path(filename)
    render_semantic_graph_image(graph, path)
    return filename, path
