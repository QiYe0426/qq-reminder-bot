from __future__ import annotations

import logging
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
GREEN_300 = "#BFE48A"
GREEN_700 = "#31583B"
TEXT_DARK = "#24351F"
TEXT_MUTED = "#6F7F62"
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
        # Netgraph 的 node_size 会乘以 BASE_SCALE=1e-2 → 15~30 pt（QQ 上可辨认）
        node_size_dict[n] = 1500 + 1500 * (w / max_node_w)
        k = node_kind.get(n, "topic")
        node_color_dict[n] = node_color(k)
        node_edge_color_dict[n] = "white"

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
        # edge_width → Netgraph 内部再乘 BASE_SCALE=1e-2 → 缩到 4~12 pt
        edge_width_dict[(u, v)] = 400 + 800 * (w / max_edge_w)
        rel = params["relation"]
        edge_color_dict[(u, v)] = edge_color(rel)
        if rel:
            edge_labels_dict[(u, v)] = rel

    # --- 使用 Netgraph 渲染到 Matplotlib figure ---
    fig, ax = plt.subplots(figsize=(8, 7), facecolor="white")
    ax.set_facecolor("white")

    # 节点标签 — 大字号确保 QQ 缩略图可读
    label_fontdict = {
        "size": 14,
        "family": plt.rcParams["font.sans-serif"][0] if plt.rcParams["font.sans-serif"] else "sans-serif",
        "color": TEXT_DARK,
        "clip_on": True,
    }
    edge_label_fontdict = {
        "size": 9,
        "family": plt.rcParams["font.sans-serif"][0] if plt.rcParams["font.sans-serif"] else "sans-serif",
        "color": TEXT_MUTED,
        "clip_on": True,
    }

    # NetworkX spring layout（稳定可靠，不在小图上崩溃）
    nx_pos = nx.spring_layout(g, seed=42, k=2.5, iterations=800, scale=2.0)
    net_pos = {n: np.array([float(p[0]), float(p[1])]) for n, p in nx_pos.items()}

    plot_instance = NetgraphPlot(
        g,
        node_layout=net_pos,
        node_size=node_size_dict,
        node_color=node_color_dict,
        node_edge_color=node_edge_color_dict,
        node_edge_width=1.0,
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
            dpi=150,
            bbox_inches="tight",
            pad_inches=0.2,
            facecolor="white",
            transparent=False,
        )
        plt.close(fig)

        if tmp.exists():
            with Image.open(tmp) as img:
                img = img.convert("RGB")
            img.save(output_png)
            logger.info("Netgraph rendered semantic graph PNG to {} ({} bytes)", output_png, output_png.stat().st_size)
        else:
            _render_empty_fallback(output_png, "语义图渲染失败")
    except Exception as exc:
        logger.exception("Netgraph rendering failed: {}", exc)
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
# 主渲染入口
# ---------------------------------------------------------------------------

def render_semantic_graph_image(graph: dict[str, object], output_path: Path) -> None:
    """渲染纯语义图 PNG — 仅 Netgraph 可视化，无额外 UI 装饰。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    nodes = graph_nodes(graph)
    has_nodes = bool(nodes)
    labels = {normalize_text(node.get("label")) for node in nodes} if has_nodes else set()
    edges = graph_edges(graph, labels) if has_nodes else []

    if not has_nodes:
        _render_empty_fallback(output_path, "暂无可视化节点")
        return

    _render_graph_via_netgraph(nodes, edges, output_path)


def render_semantic_graph_to_file(graph: dict[str, object]) -> tuple[str, Path]:
    filename = semantic_graph_image_filename(graph)
    path = semantic_graph_image_path(filename)
    render_semantic_graph_image(graph, path)
    return filename, path
