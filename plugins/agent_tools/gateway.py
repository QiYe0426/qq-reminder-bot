from __future__ import annotations

from nonebot.log import logger

from .contracts import (
    ToolResult,
    normalize_tool_result,
    parse_tool_arguments,
    tool_failure,
    validate_tool_arguments,
)
from .confirmation import (
    canonical_arguments,
    consume_confirmation,
    create_pending_confirmation,
    validate_confirmation,
)
from .registry import get_agent_tool


def tool_parameters(definition: dict[str, object]) -> object:
    function = definition.get("function")
    if not isinstance(function, dict):
        return None
    return function.get("parameters")


async def execute_tool(tool_name: str, arguments: object, context: dict[str, object]) -> ToolResult:
    tool = get_agent_tool(tool_name)
    if tool is None:
        return tool_failure(
            "tool_not_found",
            f"未知 Agent 工具：{tool_name}",
        )

    parsed_arguments, parse_error = parse_tool_arguments(arguments)
    if parse_error is not None:
        return parse_error
    assert parsed_arguments is not None

    validation_error = validate_tool_arguments(parsed_arguments, tool_parameters(tool.definition))
    if validation_error is not None:
        return validation_error

    # Imported lazily to keep registry metadata independent from the access layer.
    from plugins.agent_tool_access import authorize_agent_tool

    authorization = await authorize_agent_tool(tool_name, parsed_arguments, context)
    if not authorization.allowed:
        return tool_failure(
            authorization.error or "tool_not_allowed",
            authorization.message,
        )

    tool_context = dict(context)
    if authorization.effective_group_id:
        tool_context["_effective_group_id"] = authorization.effective_group_id

    if tool.requires_confirmation:
        confirmation_binding = {
            "tool_name": tool_name,
            "arguments": parsed_arguments,
            "user_id": str(tool_context.get("_user_id") or ""),
            "target_type": str(tool_context.get("_target_type") or ""),
            "target_id": str(tool_context.get("_target_id") or ""),
            "effective_group_id": str(tool_context.get("_effective_group_id") or ""),
        }
        confirmation_token = str(tool_context.get("_tool_confirmation_token") or "")
        if not confirmation_token:
            try:
                confirmation = await create_pending_confirmation(
                    **confirmation_binding,
                    timeout_seconds=tool.confirmation_timeout,
                )
            except Exception:
                logger.exception(f"Unable to create Agent tool confirmation: {tool_name}")
                return tool_failure(
                    "confirmation_state_failed",
                    "无法创建工具确认请求。",
                )
            arguments_summary = canonical_arguments(parsed_arguments)
            if len(arguments_summary) > 600:
                arguments_summary = arguments_summary[:599] + "…"
            message = (
                "此操作需要确认。\n"
                f"工具：{tool_name}\n"
                f"参数：{arguments_summary}\n"
                f"确认码：{confirmation.confirmation_code}\n"
                f"请回复“确认 {confirmation.confirmation_code}”。"
            )
            return tool_failure(
                "confirmation_required",
                message,
                data={
                    "confirmation_code": confirmation.confirmation_code,
                    "tool_name": tool_name,
                    "arguments_hash": confirmation.arguments_hash,
                    "expires_at": confirmation.expires_at,
                },
            )

        validation = await validate_confirmation(confirmation_token, **confirmation_binding)
        if not validation.ok:
            return tool_failure(validation.error or "confirmation_invalid", validation.message)
        consumption = await consume_confirmation(confirmation_token, **confirmation_binding)
        if not consumption.ok:
            return tool_failure(consumption.error or "confirmation_invalid", consumption.message)
        tool_context.pop("_tool_confirmation_token", None)

    try:
        result = await tool.handler(parsed_arguments, tool_context)
    except Exception:
        logger.exception(f"Agent tool handler failed: {tool_name}")
        return tool_failure(
            "tool_execution_failed",
            "工具执行失败。",
        )
    return normalize_tool_result(result)
