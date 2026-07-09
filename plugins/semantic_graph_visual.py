from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from graphviz import Digraph

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

# Graphviz 引擎选择：neato 适合力导向布局（30节点以下），fdp 适合更大的图
DEFAULT_GRAPHVIZ_ENGINE = "neato"


def _graphviz_font_name() -> str:
    """返回 Graphviz 可用的中文字体名。

    优先级：GRAPHVIZ_FONT 环境变量 → 按 OS 猜测最可能的已安装字体。
    """
    env_font = os.getenv("GRAPHVIZ_FONT")
    if env_font:
        return env_font
    system = platform.system()
    if system == "Windows":
        return "Microsoft YaHei"
    if system == "Linux":
        # 优先 fontconfig 最可能找到的 CJK 字体
        return "WenQuanYi Micro Hei"
    if system == "Darwin":
        return "PingFang SC"
    return "sans-serif"


def _dot_available() -> bool:
    """检查系统是否有 Graphviz dot 命令。"""
    return shutil.which("dot") is not None or shutil.which("dot.exe") is not None


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


# ---------------------------------------------------------------------------
# Graphviz 渲染 — 替代旧的 PIL 手绘节点/边/布局
# ---------------------------------------------------------------------------

def _gv_node_id(label: str, kind: str) -> str:
    """Graphviz 节点 ID：使用 hash 避免任何特殊字符问题。"""
    raw = f"{kind}:{label}"
    h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"n_{h}"


def _render_graph_via_graphviz(
    nodes: list[dict[str, object]],
    edges: list[dict[str, object]],
    max_weight: float,
    output_png: Path,
) -> None:
    """使用 Graphviz neato/fdp/sfdp 渲染语义图到 PNG 文件。"""
    font_name = _graphviz_font_name()
    engine = DEFAULT_GRAPHVIZ_ENGINE
    node_count = len(nodes)

    if node_count > 40:
        engine = "sfdp"
    elif node_count > 20:
        engine = "fdp"
    else:
        engine = DEFAULT_GRAPHVIZ_ENGINE

    # 估算合适的 DPI 和 size
    if engine == "sfdp":
        dpi = "120"
        size = "16,12"
    elif node_count > 20:
        dpi = "130"
        size = "12,10"
    else:
        dpi = "150"
        size = "8.5,10"

    dot = Digraph(
        name="semantic_graph",
        format="png",
        engine=engine,
        encoding="utf-8",
    )

    # 全局属性
    dot.attr(
        bgcolor=GREEN_50,
        dpi=dpi,
        size=size,
        overlap="false",
        splines="true",
        pad="0.5",
        fontname=font_name,
        nodesep="0.5",
        ranksep="0.6",
        sep="+4",
    )

    # 默认节点样式
    dot.attr(
        "node",
        fontname=font_name,
        shape="ellipse",
        style="filled,solid",
        penwidth="2",
        fontcolor=TEXT_DARK,
        color=GREEN_700,
        margin="0.08,0.02",
    )

    # 默认边样式
    dot.attr(
        "edge",
        fontname=font_name,
        fontsize="9",
        fontcolor=TEXT_MUTED,
    )

    # --- 添加节点 ---
    for node in nodes:
        label = normalize_text(node.get("label"))
        kind = normalize_text(node.get("kind") or "topic")
        weight = max(1.0, float(node.get("weight") or 1))
        nid = _gv_node_id(label, kind)
        fill = node_color(kind)
        ratio = weight / max_weight if max_weight > 0 else 0.5

        # 节点直径：0.6~1.8 英寸
        diam = 0.6 + 1.2 * ratio
        # 边框线宽：按权重增加视觉权重
        border_width = 2 + int(3 * ratio)

        dot.node(
            nid,
            label=f"{wrap_label(label, limit=10)}",
            fillcolor=fill,
            width=str(diam),
            height=str(diam),
            penwidth=str(border_width),
            fontsize=str(10 + int(6 * ratio)),  # 10~16
        )

    # --- 添加边 ---
    for edge in edges:
        source_label = normalize_text(edge.get("source"))
        source_kind = normalize_text(edge.get("source_kind") or "topic")
        target_label = normalize_text(edge.get("target"))
        target_kind = normalize_text(edge.get("target_kind") or "topic")
        src_id = _gv_node_id(source_label, source_kind)
        tgt_id = _gv_node_id(target_label, target_kind)

        if src_id == tgt_id:
            continue

        weight = max(1.0, float(edge.get("weight") or 1))
        relation = str(edge.get("relation") or "")
        rel_color = edge_color(relation)

        dot.edge(
            src_id,
            tgt_id,
            label=relation if relation else "",
            penwidth=str(max(1, min(6, 1 + int(weight / 2)))),
            color=rel_color,
            fontcolor=rel_color,
        )

    # 渲染到临时目录，避免污染 GRAPH_DIR
    temp_dir = Path(tempfile.mkdtemp(prefix="gv_"))
    try:
        source_base = temp_dir / output_png.stem
        dot.render(filename=str(source_base), cleanup=True)
        rendered = source_base.with_suffix(".png")

        if rendered.exists():
            # 移动回目标路径
            shutil.move(str(rendered), str(output_png))
        else:
            # 回退：生成空 PNG 提示
            _render_empty_fallback(output_png, "语义图渲染失败")
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


def _render_empty_fallback(path: Path, msg: str) -> None:
    """渲染失败时的占位图。"""
    img = Image.new("RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), GREEN_50)
    draw = ImageDraw.Draw(img)
    font = load_font(34, bold=True)
    draw_centered_text(draw, (CANVAS_WIDTH / 2, CANVAS_HEIGHT / 2), msg, font, TEXT_MUTED)
    img.save(path)


# ---------------------------------------------------------------------------
# 头部 + 图例 + 关系列表（保持 PIL 绘制不变）
# ---------------------------------------------------------------------------

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


def draw_top_relations(draw: ImageDraw.ImageDraw, edges: list[dict[str, object]]) -> None:
    title_font = load_font(30, bold=True)
    font_v = load_font(22)
    x0, y0, x1, y1 = 80, 1260, CANVAS_WIDTH - 80, 1430
    draw.rounded_rectangle((x0 + 6, y0 + 8, x1 + 6, y1 + 8), radius=32, fill="#E4F0C9")
    draw.rounded_rectangle((x0, y0, x1, y1), radius=32, fill=CARD_BG, outline=CARD_BORDER, width=3)
    draw.text((x0 + 32, y0 + 24), "主要关系", font=title_font, fill=GREEN_700)
    if not edges:
        draw.text((x0 + 32, y0 + 75), "暂无关系。请确认所选范围内有足够消息。", font=font_v, fill=TEXT_MUTED)
        return
    for index, edge in enumerate(edges[:4]):
        line = (
            f"{edge.get('source')} --{edge.get('relation')}→ {edge.get('target')}"
            f"（{float(edge.get('weight') or 1):.0f}）"
        )
        draw.text((x0 + 32, y0 + 72 + index * 30), line[:58], font=font_v, fill=TEXT_DARK)


# ---------------------------------------------------------------------------
# 主渲染入口
# ---------------------------------------------------------------------------

def render_semantic_graph_image(graph: dict[str, object], output_path: Path) -> None:
    """渲染语义图 PNG。

    使用 Graphviz 进行力导向布局和节点/边绘制，PIL 进行标题/图例/关系列表合成。
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    BODY_TOP = 270
    BODY_BOTTOM = 1168
    body_height = BODY_BOTTOM - BODY_TOP

    canvas = Image.new("RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), GREEN_50)
    draw = ImageDraw.Draw(canvas)

    draw_header(draw, graph)
    draw.rounded_rectangle(
        (58, BODY_TOP, CANVAS_WIDTH - 58, BODY_BOTTOM),
        radius=42, fill=CARD_BG, outline=CARD_BORDER, width=4,
    )
    draw_legend(draw)

    nodes = graph_nodes(graph)
    has_nodes = bool(nodes)
    labels = {(normalize_text(node.get("label")), normalize_text(node.get("kind") or "topic")) for node in nodes}
    edges = graph_edges(graph, labels)

    if has_nodes and _dot_available():
        # ---- Graphviz 渲染 ----
        max_weight = max([float(n.get("weight") or 1) for n in nodes] or [1.0])

        gv_png = output_path.parent / f".gv_{output_path.stem}.png"
        try:
            _render_graph_via_graphviz(nodes, edges, max_weight, gv_png)

            if gv_png.exists():
                gv_img = Image.open(gv_png)
                # 适应画布主体区域
                margin = 40
                max_w = CANVAS_WIDTH - 58 * 2 - margin * 2
                max_h = body_height - margin * 2
                gv_img.thumbnail((max_w, max_h), Image.LANCZOS)
                paste_x = (CANVAS_WIDTH - gv_img.width) // 2
                paste_y = BODY_TOP + (body_height - gv_img.height) // 2
                canvas.paste(gv_img, (paste_x, paste_y))
                gv_path = Path(str(gv_png))
                if gv_path.exists():
                    gv_path.unlink()
        except Exception:
            # Graphviz 异常回退：在主体区域显示提示
            empty_font = load_font(30)
            draw_centered_text(draw, (CANVAS_WIDTH / 2, BODY_TOP + body_height // 2 - 20),
                               "图形渲染异常，请稍后重试", empty_font, TEXT_MUTED)
            if gv_png.exists():
                gv_path = Path(str(gv_png))
                if gv_path.exists():
                    gv_path.unlink()
    elif has_nodes and not _dot_available():
        # dot 不可用：提示安装
        empty_font = load_font(30)
        draw_centered_text(draw, (CANVAS_WIDTH / 2, BODY_TOP + body_height // 2 - 20),
                           "需要 Graphviz（sudo apt install graphviz）", empty_font, TEXT_MUTED)
    else:
        # 无节点
        empty_font = load_font(34, bold=True)
        draw_centered_text(draw, (CANVAS_WIDTH / 2, BODY_TOP + body_height // 2),
                           "暂无可视化节点", empty_font, TEXT_MUTED)

    draw_top_relations(draw, edges)
    canvas.save(output_path)


def render_semantic_graph_to_file(graph: dict[str, object]) -> tuple[str, Path]:
    filename = semantic_graph_image_filename(graph)
    path = semantic_graph_image_path(filename)
    render_semantic_graph_image(graph, path)
    return filename, path
