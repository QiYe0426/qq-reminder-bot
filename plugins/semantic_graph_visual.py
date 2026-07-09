from __future__ import annotations

import logging
import os
import platform
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

# matplotlib.use("Agg") MUST be the very first matplotlib operation,
# before any import triggers backend selection.
import matplotlib as mpl
mpl.use("Agg")
import matplotlib.font_manager as fm  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

import numpy as np
import networkx as nx
from netgraph import Graph as NetgraphPlot
from PIL import Image, ImageDraw

from plugins.daily_report_visual import load_font

logger = logging.getLogger(__name__)

GRAPH_DIR = Path("data/semantic_graphs")
CANVAS_WIDTH = 1280
CANVAS_HEIGHT = 1500
GREEN_50 = "#F7FCEB"
GREEN_100 = "#EEF8D7"
GREEN_300 = "#BFE48A"
GREEN_700 = "#31583B"
TEXT_DARK = "#24351F"
TEXT_MUTED = "#6F7F62"
CARD_BG = "#FFFFF8"
CARD_BORDER = "#D4EAA5"
GOLD = "#F0C955"
BLUE = "#78A8D8"
PINK = "#E8A7B7"

# ---- Matplotlib 中文字体配置（全局一次） ----
_MPL_CJK_CONFIGURED = False


def _configure_mpl_cjk() -> None:
    """配置 Matplotlib 渲染中文所需的 CJK 字体 Fallback 链。"""
    global _MPL_CJK_CONFIGURED
    if _MPL_CJK_CONFIGURED:
        return
    system = platform.system()
    fallback_candidates: list[str] = []
    if system == "Windows":
        fallback_candidates = ["Microsoft YaHei", "SimHei", "DengXian"]
    elif system == "Linux":
        fallback_candidates = [
            "WenQuanYi Micro Hei",
            "WenQuanYi Zen Hei",
            "Noto Sans CJK SC",
            "Noto Sans CJK JP",
            "Source Han Sans SC",
        ]
    elif system == "Darwin":
        fallback_candidates = ["PingFang SC", "Heiti SC", "STHeiti"]

    # 尝试第一个可用的字体
    chosen = None
    for name in fallback_candidates:
        try:
            fp = fm.findfont(name, fallback_to_default=False)
            if fp:
                chosen = name
                break
        except Exception:
            continue
    if chosen:
        plt.rcParams["font.sans-serif"] = [chosen] + plt.rcParams.get("font.sans-serif", [])
    plt.rcParams["axes.unicode_minus"] = False
    _MPL_CJK_CONFIGURED = True


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


def graph_nodes(graph: dict[str, object], *, limit: int = 32) -> list[dict[str, object]]:
    nodes = [item for item in graph.get("nodes", []) if isinstance(item, dict)]
    nodes.sort(key=lambda item: float(item.get("weight") or 1), reverse=True)
    return nodes[:limit]


def graph_edges(graph: dict[str, object], node_labels: set[str], *, limit: int = 70) -> list[dict[str, object]]:
    edges = []
    for item in graph.get("edges", []):
        if not isinstance(item, dict):
            continue
        source = normalize_text(item.get("source"))
        target = normalize_text(item.get("target"))
        if source in node_labels and target in node_labels:
            edges.append(item)
    edges.sort(key=lambda item: float(item.get("weight") or 1), reverse=True)
    return edges[:limit]


# ---------------------------------------------------------------------------
# Netgraph 渲染 — 以 Netgraph + Matplotlib 替代旧的 Graphviz 渲染
# ---------------------------------------------------------------------------

def _render_graph_via_netgraph(
    nodes: list[dict[str, object]],
    edges: list[dict[str, object]],
    output_png: Path,
) -> None:
    """使用 Netgraph (publication-quality Matplotlib) 渲染语义图到 PNG。

    Netgraph 提供 Fruchterman-Reingold 力导向布局、标签碰撞回避、
    曲线边避免节点遮挡等，生成出版级质量的网络图。
    """
    _configure_mpl_cjk()

    # --- 构建 NetworkX 图 ---
    g = nx.Graph()
    node_kind: dict[str, str] = {}
    node_weight: dict[str, float] = {}

    for node in nodes:
        label = normalize_text(node.get("label"))
        kind = normalize_text(node.get("kind") or "topic")
        weight = max(1.0, float(node.get("weight") or 1))
        if not label:
            continue
        # 加入 networkx（使用原始 label 作为节点 ID 以保证唯一性）
        g.add_node(label)
        node_kind[label] = kind
        node_weight[label] = weight

    if g.number_of_nodes() == 0:
        _render_empty_fallback(output_png, "暂无可视化节点")
        return

    max_node_w = max(node_weight.values()) if node_weight else 1.0

    for edge in edges:
        src = normalize_text(edge.get("source"))
        tgt = normalize_text(edge.get("target"))
        if src in g and tgt in g and src != tgt:
            g.add_edge(src, tgt)

    # --- 节点视觉参数 ---
    node_size_dict: dict[str, float] = {}
    node_color_dict: dict[str, str] = {}
    node_edge_color_dict: dict[str, str] = {}

    for n in g.nodes():
        w = node_weight.get(n, 1.0)
        # Netgraph 的 node_size 会乘以 BASE_SCALE=1e-2
        # 所以这里给 2000~6000 让它缩放到 20~60 pt
        node_size_dict[n] = 2000 + 4000 * (w / max_node_w)
        k = node_kind.get(n, "topic")
        node_color_dict[n] = node_color(k)
        node_edge_color_dict[n] = GREEN_700

    # --- 边视觉参数 ---
    # nx.Graph 是无向图，g.edges() 按节点插入顺序访问邻接表，
    # 因此边 tuple 的方向与 Netgraph 内部的 edge list 一致。
    # 用 frozenset 做不区分方向的查找，再用 g.edges() 的 key 填充 dict。
    max_edge_w = 1.0
    edge_params_lookup: dict[frozenset, dict] = {}
    for edge in edges:
        src = normalize_text(edge.get("source"))
        tgt = normalize_text(edge.get("target"))
        if src not in g or tgt not in g or src == tgt:
            continue
        w = max(1.0, float(edge.get("weight") or 1))
        if w > max_edge_w:
            max_edge_w = w
        rel = str(edge.get("relation") or "")
        edge_params_lookup[frozenset((src, tgt))] = {"weight": w, "relation": rel}

    edge_width_dict: dict[tuple[str, str], float] = {}
    edge_color_dict: dict[tuple[str, str], str] = {}
    edge_labels_dict: dict[tuple[str, str], str] = {}

    for u, v in g.edges():
        params = edge_params_lookup.get(frozenset((u, v)), {"weight": 1.0, "relation": ""})
        w = max(1.0, params["weight"])
        # edge_width → Netgraph 内部再乘 BASE_SCALE=1e-2，所以给 800~3000 → 缩到 8~30 pt
        edge_width_dict[(u, v)] = 800 + 2200 * (w / max_edge_w)
        rel = params["relation"]
        edge_color_dict[(u, v)] = edge_color(rel)
        if rel:
            edge_labels_dict[(u, v)] = rel

    # --- 使用 Netgraph 渲染到 Matplotlib figure ---
    fig, ax = plt.subplots(figsize=(10, 8), facecolor=GREEN_50)
    ax.set_facecolor(GREEN_50)

    # 标签字体
    label_fontdict = {
        "size": 8,
        "family": plt.rcParams["font.sans-serif"][0] if plt.rcParams["font.sans-serif"] else "sans-serif",
        "color": TEXT_DARK,
    }
    edge_label_fontdict = {
        "size": 6,
        "family": plt.rcParams["font.sans-serif"][0] if plt.rcParams["font.sans-serif"] else "sans-serif",
        "color": TEXT_MUTED,
        "bbox": {"boxstyle": "round,pad=0.2", "facecolor": "white", "edgecolor": "#ddd", "alpha": 0.85},
    }

    # 预计算 spring layout positions（NetworkX 实现），避免 Netgraph 内部 Voronoi 在小图上崩溃
    nx_pos = nx.spring_layout(g, seed=42, k=8.0, iterations=500)
    # 转成 Netgraph 需要的格式（np.ndarray）
    net_pos = {n: np.array([float(p[0]), float(p[1])]) for n, p in nx_pos.items()}

    NetgraphPlot(
        g,
        node_layout=net_pos,  # dict → fixed layout, 跳过 Netgraph 内部的 spring+Voronoi（小图易崩溃）
        node_size=node_size_dict,
        node_color=node_color_dict,
        node_edge_color=node_edge_color_dict,
        node_edge_width=0.4,
        node_labels=True,
        node_label_fontdict=label_fontdict,
        edge_color=edge_color_dict,
        edge_width=edge_width_dict,
        edge_labels=edge_labels_dict if edge_labels_dict else False,
        edge_label_fontdict=edge_label_fontdict,
        edge_layout="curved",
        arrows=True,
        ax=ax,
    )

    ax.axis("off")

    # 保存到临时 PNG
    tmp = Path(tempfile.mktemp(suffix=".png"))
    try:
        fig.savefig(
            str(tmp),
            dpi=200,
            bbox_inches="tight",
            pad_inches=0.3,
            facecolor=GREEN_50,
            transparent=False,
        )
        plt.close(fig)

        if tmp.exists():
            with Image.open(tmp) as img:
                img = img.convert("RGB")
            img.save(output_png)
            logger.info("Netgraph rendered semantic graph PNG to %s (%d bytes)", output_png, output_png.stat().st_size)
        else:
            _render_empty_fallback(output_png, "语义图渲染失败")
    except Exception as exc:
        logger.exception("Netgraph rendering failed: %s", exc)
        _render_empty_fallback(output_png, "图形渲染异常，请稍后重试")
        plt.close(fig)
    finally:
        tmp.unlink(missing_ok=True)


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
        src = normalize_text(edge.get("source"))
        tgt = normalize_text(edge.get("target"))
        rel = normalize_text(edge.get("relation"))
        line = f"{src} --{rel}→ {tgt}（{float(edge.get('weight') or 1):.0f}）"
        draw.text((x0 + 32, y0 + 72 + index * 30), line[:58], font=font_v, fill=TEXT_DARK)


# ---------------------------------------------------------------------------
# 主渲染入口
# ---------------------------------------------------------------------------

def render_semantic_graph_image(graph: dict[str, object], output_path: Path) -> None:
    """渲染语义图 PNG。

    使用 Netgraph (Matplotlib) 进行力导向布局和节点/边绘制，PIL 进行标题/图例/关系列表合成。
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
    labels = {normalize_text(node.get("label")) for node in nodes}
    edges = graph_edges(graph, labels)

    if has_nodes:
        # ---- Netgraph 渲染 ----
        ng_png = output_path.parent / f".ng_{output_path.stem}.png"
        try:
            _render_graph_via_netgraph(nodes, edges, ng_png)

            if ng_png.exists():
                with Image.open(ng_png) as ng_img:
                    # 适应画布主体区域
                    margin = 40
                    max_w = CANVAS_WIDTH - 58 * 2 - margin * 2
                    max_h = body_height - margin * 2
                    ng_img.thumbnail((max_w, max_h), Image.LANCZOS)
                    paste_x = (CANVAS_WIDTH - ng_img.width) // 2
                    paste_y = BODY_TOP + (body_height - ng_img.height) // 2
                    canvas.paste(ng_img, (paste_x, paste_y))
                ng_png.unlink(missing_ok=True)
            else:
                empty_font = load_font(30)
                draw_centered_text(draw, (CANVAS_WIDTH / 2, BODY_TOP + body_height // 2 - 20),
                                   "图形渲染失败", empty_font, TEXT_MUTED)
        except Exception:
            logger.exception("Netgraph rendering failed in compositing")
            empty_font = load_font(30)
            draw_centered_text(draw, (CANVAS_WIDTH / 2, BODY_TOP + body_height // 2 - 20),
                               "图形渲染异常，请稍后重试", empty_font, TEXT_MUTED)
            ng_png.unlink(missing_ok=True)
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
