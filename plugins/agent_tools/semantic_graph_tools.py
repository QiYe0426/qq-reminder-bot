from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from nonebot.log import logger

from plugins.access_control import FEATURE_COLLECTOR, is_group_feature_enabled
from plugins.semantic_graph import (
    DEFAULT_GRAPH_LIMIT,
    build_semantic_graph_result,
    latest_semantic_graph,
    load_semantic_graph,
    semantic_graph_summary,
)
from plugins.semantic_graph_visual import render_semantic_graph_to_file

from .registry import AgentTool, AgentToolContext, AgentToolResult


def _tool_definition(
    *,
    name: str,
    description: str,
    properties: dict[str, object],
    required: list[str] | None = None,
) -> dict[str, object]:
    parameters: dict[str, object] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        parameters["required"] = required
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


def current_group_id(context: AgentToolContext) -> str:
    if str(context.get("_target_type") or "") != "group":
        return ""
    return str(context.get("_target_id") or "").strip()


def target_group_id(args: dict[str, object], context: AgentToolContext) -> str:
    del args
    return str(context.get("_effective_group_id") or "").strip() or current_group_id(context)


def parse_graph_date(value: object) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    today = date.today()
    if raw in {"今天", "今日", "today"}:
        return today
    if raw in {"昨天", "昨日", "yesterday"}:
        return today - timedelta(days=1)
    if raw in {"前天"}:
        return today - timedelta(days=2)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("日期格式请用 今天 / 昨天 / 前天 / YYYY-MM-DD。") from exc


def graph_limit(args: dict[str, object]) -> int:
    try:
        return int(args.get("limit") or DEFAULT_GRAPH_LIMIT)
    except (TypeError, ValueError):
        return DEFAULT_GRAPH_LIMIT


async def ensure_collector_available(group_id: str) -> tuple[bool, str]:
    if not await is_group_feature_enabled(group_id, FEATURE_COLLECTOR):
        return False, "语义图依赖消息采集。请先为这个群开启消息采集。"
    return True, ""


async def build_graph_from_args(args: dict[str, object], context: AgentToolContext) -> AgentToolResult:
    group_id = target_group_id(args, context)
    if not group_id:
        return {"ok": False, "error": "missing_group_id", "message": "请提供要操作的群号，例如 group_id=722290838。"}
    allowed, reason = await ensure_collector_available(group_id)
    if not allowed:
        return {"ok": False, "error": "feature_unavailable", "message": reason}
    try:
        target_date = parse_graph_date(args.get("date"))
    except ValueError as exc:
        return {"ok": False, "error": "invalid_date", "message": str(exc)}
    keyword = str(args.get("keyword") or "").strip()
    return await build_semantic_graph_result(
        group_id=group_id,
        target_date=target_date,
        keyword=keyword,
        limit=graph_limit(args),
    )


async def build_semantic_graph_tool(args: dict[str, object], context: AgentToolContext) -> AgentToolResult:
    graph = await build_graph_from_args(args, context)
    if not graph.get("ok"):
        return graph
    summary = semantic_graph_summary(graph)
    return {
        **graph,
        "summary": summary,
        "message": f"已生成语义图：{graph.get('node_count', 0)} 个节点，{graph.get('edge_count', 0)} 条关系。",
    }


async def load_authorized_semantic_graph(
    args: dict[str, object],
    context: AgentToolContext,
    *,
    not_found_error: str,
) -> AgentToolResult:
    group_id = target_group_id(args, context)
    if not group_id:
        return {"ok": False, "error": "missing_group_id", "message": "请提供要操作的群号，例如 group_id=722290838。"}
    allowed, reason = await ensure_collector_available(group_id)
    if not allowed:
        return {"ok": False, "error": "feature_unavailable", "message": reason}

    graph_id = str(args.get("graph_id") or "").strip()
    graph = await load_semantic_graph(graph_id) if graph_id else await latest_semantic_graph(group_id)
    if graph is not None and str(graph.get("group_id") or "") != group_id:
        return {
            "ok": False,
            "error": "group_permission_denied",
            "message": "指定的语义图不属于已授权的目标群。",
        }
    if graph is None:
        return {
            "ok": False,
            "error": not_found_error,
            "message": "还没有这个群的语义图。可以先调用 build_semantic_graph 生成。",
        }
    return graph


async def get_semantic_graph_tool(args: dict[str, object], context: AgentToolContext) -> AgentToolResult:
    graph = await load_authorized_semantic_graph(args, context, not_found_error="not_found")
    if not graph.get("ok"):
        return graph
    summary = semantic_graph_summary(graph)
    return {
        **graph,
        "summary": summary,
        "message": "已读取语义图。",
    }


async def send_graph_image_to_context(
    context: AgentToolContext,
    source_path: Path,
    filename: str,
) -> None:
    from nonebot import get_bot
    from nonebot.adapters.onebot.v11 import Message, MessageSegment
    from plugins.message_collector import export_file_path, export_file_url

    bot = get_bot()
    export_path = export_file_path(filename)
    export_path.parent.mkdir(parents=True, exist_ok=True)
    export_path.write_bytes(source_path.read_bytes())
    file_value = export_file_url(filename)
    target_type = str(context.get("_target_type") or "")
    target_id = str(context.get("_target_id") or "")
    user_id = str(context.get("_user_id") or target_id or "").strip()

    if target_type == "group" and target_id.isdigit():
        try:
            await bot.call_api("send_group_msg", group_id=int(target_id), message=Message(MessageSegment.image(file=file_value)))
        except Exception:
            await bot.call_api("send_group_msg", group_id=int(target_id), message=Message(MessageSegment.image(file=str(export_path))))
        return

    if user_id.isdigit():
        try:
            await bot.call_api("send_private_msg", user_id=int(user_id), message=Message(MessageSegment.image(file=file_value)))
        except Exception:
            await bot.call_api("send_private_msg", user_id=int(user_id), message=Message(MessageSegment.image(file=str(export_path))))


async def render_semantic_graph_tool(args: dict[str, object], context: AgentToolContext) -> AgentToolResult:
    graph_result = await load_authorized_semantic_graph(args, context, not_found_error="graph_not_found")
    if not graph_result.get("ok"):
        return graph_result

    filename, path = render_semantic_graph_to_file(graph_result)
    sent = False
    send_image = args.get("send_image")
    should_send = True if send_image is None else bool(send_image)
    error = ""
    if should_send:
        try:
            await send_graph_image_to_context(context, path, filename)
            sent = True
        except Exception as exc:
            logger.exception("Failed to send semantic graph image")
            error = repr(exc)

    return {
        "ok": True,
        "graph_id": graph_result.get("graph_id"),
        "group_id": graph_result.get("group_id"),
        "image_filename": filename,
        "image_path": str(path),
        "sent": sent,
        "send_error": error,
        "summary": graph_result.get("summary") or semantic_graph_summary(graph_result),
        "message": "语义图可视化已生成。" + (" 已发送图片。" if sent else " 图片未自动发送，可在服务器文件中查看。"),
    }


GROUP_ID_PROPERTY = {
    "group_id": {
        "type": "string",
        "description": "QQ group id. Required in private chat; optional in a group chat.",
    },
}

BUILD_GRAPH_PROPERTIES = {
    **GROUP_ID_PROPERTY,
    "date": {
        "type": "string",
        "description": "Optional graph date: 今天, 昨天, 前天, or YYYY-MM-DD. Empty means recent messages.",
    },
    "keyword": {
        "type": "string",
        "description": "Optional keyword filter for the messages used to build the graph.",
    },
    "limit": {
        "type": "integer",
        "description": "Maximum source message count. Defaults to 240 and is capped internally.",
    },
}


SEMANTIC_GRAPH_TOOLS = [
    AgentTool(
        name="build_semantic_graph",
        category="semantic_graph",
        requires_feature=FEATURE_COLLECTOR,
        requires_admin=True,
        group_scope="private_explicit",
        requires_target_group_admin=True,
        side_effect="write",
        risk_level="high",
        requires_confirmation=True,
        confirmation_timeout=180,
        idempotency_enabled=True,
        idempotency_ttl=120,
        idempotency_lease_timeout=300,
        idempotency_temporary_errors=frozenset({"feature_unavailable"}),
        definition=_tool_definition(
            name="build_semantic_graph",
            description=(
                "Build or refresh a semantic graph for a QQ group from archived collected messages. "
                "Admin only. Use it when the user asks to generate/update a topic relationship map, semantic graph, or group knowledge structure."
            ),
            properties=BUILD_GRAPH_PROPERTIES,
        ),
        handler=build_semantic_graph_tool,
    ),
    AgentTool(
        name="get_semantic_graph",
        category="semantic_graph",
        requires_feature=FEATURE_COLLECTOR,
        requires_admin=True,
        group_scope="private_explicit",
        requires_target_group_admin=True,
        definition=_tool_definition(
            name="get_semantic_graph",
            description=(
                "Read the latest or specified semantic graph for a QQ group. "
                "Returns important nodes and relationships. Admin only. If no graph exists, call build_semantic_graph first."
            ),
            properties={
                **GROUP_ID_PROPERTY,
                "graph_id": {
                    "type": "string",
                    "description": "Optional exact graph id. Leave empty to read the latest graph for the group.",
                },
            },
        ),
        handler=get_semantic_graph_tool,
    ),
    AgentTool(
        name="render_semantic_graph",
        category="semantic_graph_visual",
        requires_feature=FEATURE_COLLECTOR,
        requires_admin=True,
        group_scope="private_explicit",
        requires_target_group_admin=True,
        side_effect="external",
        risk_level="high",
        requires_confirmation=True,
        confirmation_timeout=180,
        idempotency_enabled=True,
        idempotency_ttl=300,
        idempotency_lease_timeout=120,
        idempotency_temporary_failure_ttl=15,
        idempotency_temporary_errors=frozenset({"graph_not_found"}),
        definition=_tool_definition(
            name="render_semantic_graph",
            description=(
                "Render a semantic graph visualization image for a QQ group and send it to the current chat when possible. "
                "Admin only. Use it when the user asks for 可视化语义图 / 画出来 / 语义关系图."
            ),
            properties={
                **GROUP_ID_PROPERTY,
                "graph_id": {
                    "type": "string",
                    "description": "Optional exact graph id. Leave empty to render the latest existing graph.",
                },
                "send_image": {
                    "type": "boolean",
                    "description": "Whether to send the generated image into QQ. Defaults to true.",
                },
            },
        ),
        handler=render_semantic_graph_tool,
    ),
]
