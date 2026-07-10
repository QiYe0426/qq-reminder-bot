from __future__ import annotations

from nonebot.log import logger

from .contracts import (
    ToolResult,
    normalize_tool_result,
    parse_tool_arguments,
    tool_failure,
    validate_tool_arguments,
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

    try:
        result = await tool.handler(parsed_arguments, tool_context)
    except Exception:
        logger.exception(f"Agent tool handler failed: {tool_name}")
        return tool_failure(
            "tool_execution_failed",
            "工具执行失败。",
        )
    return normalize_tool_result(result)
