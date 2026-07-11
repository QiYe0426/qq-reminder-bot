from __future__ import annotations

from plugins.access_control import FEATURE_AI_CHAT, FEATURE_COLLECTOR, is_group_feature_enabled
from plugins.group_context_service import group_context_result

from .registry import AgentTool, AgentToolContext, AgentToolMetadata, AgentToolResult


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


async def get_group_context_tool(args: dict[str, object], context: AgentToolContext) -> AgentToolResult:
    target_type = str(context.get("_target_type") or "")
    group_id = str(context.get("_target_id") or "")
    if target_type != "group" or not group_id:
        return {
            "ok": False,
            "error": "not_group_chat",
            "message": "群上下文工具只能在群聊中使用。",
        }

    raw_limit = args.get("limit")
    limit = int(raw_limit) if isinstance(raw_limit, (int, float, str)) and str(raw_limit).isdigit() else 20
    keyword = str(args.get("keyword") or "").strip()
    event = context.get("_event")
    exclude_message_id = str(getattr(event, "message_id", "") or "")
    collector_enabled = await is_group_feature_enabled(group_id, FEATURE_COLLECTOR)
    return await group_context_result(
        group_id=group_id,
        collector_enabled=collector_enabled,
        limit=limit,
        keyword=keyword,
        exclude_message_id=exclude_message_id,
    )


GROUP_CONTEXT_TOOLS = [
    AgentTool(
        name="get_group_context",
        metadata=AgentToolMetadata(side_effect="read", resource_scope="current_group"),
        category="group_context",
        requires_feature=FEATURE_AI_CHAT,
        requires_group=True,
        definition=_tool_definition(
            name="get_group_context",
            description=(
                "Get recent or keyword-filtered context from the current QQ group. "
                "When message collection is enabled it reads archived group messages; otherwise it reads only the temporary in-process recent context."
            ),
            properties={
                "keyword": {
                    "type": "string",
                    "description": "Optional keyword to search in group messages. Leave empty for the latest messages.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum message count. Defaults to 20 and is capped internally.",
                },
            },
        ),
        handler=get_group_context_tool,
    ),
]
