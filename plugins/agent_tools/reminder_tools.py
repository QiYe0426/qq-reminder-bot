from __future__ import annotations

from plugins.access_control import FEATURE_AI_CHAT
from plugins.reminder_service import (
    ReminderScope,
    ReminderTarget,
    cancel_reminder,
    create_reminder,
    list_reminders_result,
)

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


async def create_reminder_tool(args: dict[str, object], context: AgentToolContext) -> AgentToolResult:
    scope = context.get("_scope")
    if not isinstance(scope, ReminderScope):
        return {
            "ok": False,
            "error": "missing_event",
            "message": "缺少当前会话上下文。",
        }
    target_user_id = str(args.get("target_user_id") or "").strip()
    target_display_name = str(args.get("target_display_name") or "").strip()
    target = ReminderTarget(target_user_id, target_display_name) if target_user_id else None
    content_override = str(args.get("content_override") or "").strip() or None
    return await create_reminder(
        scope,
        str(args.get("text") or ""),
        target=target,
        content_override=content_override,
    )


async def list_reminders_tool(args: dict[str, object], context: AgentToolContext) -> AgentToolResult:
    user_id = str(context.get("_user_id") or "")
    if not user_id:
        return {
            "ok": False,
            "error": "missing_user",
            "message": "缺少当前用户。",
        }
    limit = args.get("limit")
    limit_value = int(limit) if isinstance(limit, (int, float, str)) and str(limit).isdigit() else 10
    return await list_reminders_result(user_id, limit=limit_value)


async def cancel_reminder_tool(args: dict[str, object], context: AgentToolContext) -> AgentToolResult:
    user_id = str(context.get("_user_id") or "")
    if not user_id:
        return {
            "ok": False,
            "error": "missing_user",
            "message": "缺少当前用户。",
        }
    reminder_id = args.get("reminder_id")
    if not isinstance(reminder_id, (int, float, str)) or not str(reminder_id).isdigit():
        return {
            "ok": False,
            "error": "invalid_id",
            "message": "提醒编号无效。",
        }
    return await cancel_reminder(user_id, int(reminder_id))


REMINDER_TOOLS = [
    AgentTool(
        name="create_reminder",
        category="reminder",
        requires_feature=FEATURE_AI_CHAT,
        definition=_tool_definition(
            name="create_reminder",
            description="Create a reminder for the current user in the current chat. Use the same reminder text a human would send after '提醒'.",
            properties={
                "text": {
                    "type": "string",
                    "description": "Reminder text such as '09:00 喝水', '10分钟后吃饭', or '明天这个时候吃饭'.",
                },
                "target_user_id": {
                    "type": "string",
                    "description": "Optional QQ user id to remind. Leave empty for reminding the requester.",
                },
                "target_display_name": {
                    "type": "string",
                    "description": "Optional display name for the reminder target.",
                },
                "content_override": {
                    "type": "string",
                    "description": "Optional cleaned reminder content after removing the target name.",
                },
            },
            required=["text"],
        ),
        handler=create_reminder_tool,
    ),
    AgentTool(
        name="list_reminders",
        category="reminder",
        requires_feature=FEATURE_AI_CHAT,
        definition=_tool_definition(
            name="list_reminders",
            description="List the current user's unfinished reminders.",
            properties={
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of reminders to list.",
                },
            },
        ),
        handler=list_reminders_tool,
    ),
    AgentTool(
        name="cancel_reminder",
        category="reminder",
        requires_feature=FEATURE_AI_CHAT,
        definition=_tool_definition(
            name="cancel_reminder",
            description="Cancel one unfinished reminder by id for the current user.",
            properties={
                "reminder_id": {
                    "type": "integer",
                    "description": "Reminder id to cancel.",
                },
            },
            required=["reminder_id"],
        ),
        handler=cancel_reminder_tool,
    ),
]
