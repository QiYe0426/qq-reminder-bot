from __future__ import annotations

from plugins.access_control import FEATURE_AI_CHAT
from plugins.reminder_service import (
    ReminderScope,
    cancel_reminder,
    create_reminder,
    list_reminders_result,
)

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


async def create_reminder_tool(args: dict[str, object], context: AgentToolContext) -> AgentToolResult:
    scope = context.get("_scope")
    if not isinstance(scope, ReminderScope):
        return {
            "ok": False,
            "error": "missing_event",
            "message": "缺少当前会话上下文。",
        }
    current_user_id = str(context.get("_user_id") or "").strip()
    if not current_user_id or str(scope.user_id) != current_user_id:
        return {
            "ok": False,
            "error": "invalid_identity_scope",
            "message": "当前用户身份与提醒会话不一致。",
        }
    content_override = str(args.get("content_override") or "").strip() or None
    return await create_reminder(
        scope,
        str(args.get("text") or ""),
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
        metadata=AgentToolMetadata(
            risk_level="medium",
            side_effect="database_write",
            resource_scope="user",
            confirmation_policy="optional",
            idempotency_policy="result_cache",
        ),
        category="reminder",
        requires_feature=FEATURE_AI_CHAT,
        side_effect="write",
        risk_level="medium",
        idempotency_enabled=True,
        idempotency_ttl=120,
        idempotency_lease_timeout=30,
        definition=_tool_definition(
            name="create_reminder",
            description="Create a reminder for the current user in the current chat. Use the same reminder text a human would send after '提醒'.",
            properties={
                "text": {
                    "type": "string",
                    "description": "Reminder text such as '09:00 喝水', '10分钟后吃饭', or '明天这个时候吃饭'.",
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
        metadata=AgentToolMetadata(side_effect="read", resource_scope="user"),
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
        metadata=AgentToolMetadata(
            risk_level="medium",
            side_effect="database_write",
            resource_scope="user",
            confirmation_policy="optional",
            idempotency_policy="result_cache",
        ),
        category="reminder",
        requires_feature=FEATURE_AI_CHAT,
        side_effect="write",
        risk_level="medium",
        idempotency_enabled=True,
        idempotency_ttl=300,
        idempotency_lease_timeout=30,
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
