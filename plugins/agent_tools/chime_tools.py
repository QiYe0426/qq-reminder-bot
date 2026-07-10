from __future__ import annotations

from plugins.chime_service import CHIME_MODE_HOURLY, set_chime_state

from .registry import AgentTool, AgentToolContext, AgentToolResult


SET_CHIME_DEFINITION: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "set_chime",
        "description": "Enable or disable the hourly chime for the current chat. Group chats still require admin permission.",
        "parameters": {
            "type": "object",
            "properties": {
                "enabled": {
                    "type": "boolean",
                    "description": "Whether to enable the chime.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["hourly", "twice_daily"],
                    "description": "Chime mode when enabled.",
                },
            },
            "required": ["enabled"],
            "additionalProperties": False,
        },
    },
}


async def set_chime_tool(args: dict[str, object], context: AgentToolContext) -> AgentToolResult:
    target_type = str(context.get("_target_type") or "")
    target_id = str(context.get("_target_id") or "")
    if not target_type or not target_id:
        return {
            "ok": False,
            "error": "missing_target",
            "message": "缺少当前会话信息。",
        }
    return await set_chime_state(
        target_type,
        target_id,
        bool(args.get("enabled")),
        str(args.get("mode") or CHIME_MODE_HOURLY),
    )


CHIME_TOOLS = [
    AgentTool(
        name="set_chime",
        category="chime",
        requires_admin=True,
        side_effect="external",
        risk_level="high",
        requires_confirmation=True,
        confirmation_timeout=120,
        definition=SET_CHIME_DEFINITION,
        handler=set_chime_tool,
    )
]
