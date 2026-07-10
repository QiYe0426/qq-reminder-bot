from __future__ import annotations

from nonebot.log import logger

from .contracts import (
    ToolResult,
    normalize_tool_result,
    parse_tool_arguments,
    tool_failure,
    validate_tool_arguments,
)
from .call_identity import canonical_arguments
from .confirmation import (
    consume_confirmation,
    create_pending_confirmation,
    validate_confirmation,
)
from .idempotency import (
    classify_failure,
    claim_execution,
    complete_execution,
    create_execution_binding,
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

    execution_binding = None
    execution_owner_token = ""
    if tool.idempotency_enabled:
        execution_binding = create_execution_binding(
            tool_name=tool_name,
            arguments=parsed_arguments,
            user_id=str(tool_context.get("_user_id") or ""),
            target_type=str(tool_context.get("_target_type") or ""),
            target_id=str(tool_context.get("_target_id") or ""),
            effective_group_id=str(tool_context.get("_effective_group_id") or ""),
        )
        try:
            claim = await claim_execution(
                execution_binding,
                ttl_seconds=tool.idempotency_ttl,
                lease_seconds=tool.idempotency_lease_timeout,
            )
        except Exception:
            logger.exception(f"Unable to claim Agent tool execution: {tool_name}")
            return tool_failure(
                "idempotency_unavailable",
                "无法建立工具重复执行保护，操作未执行。",
                retryable=True,
            )
        if claim.action == "cached" and claim.record is not None and claim.record.result is not None:
            return claim.record.result
        if claim.action == "running":
            return tool_failure(
                "already_running",
                "相同工具调用正在执行，请稍后查看结果。",
                retryable=True,
            )
        if claim.action == "unknown":
            return tool_failure(
                "execution_state_unknown",
                "相同工具调用的执行状态无法确定，为避免重复副作用，未再次执行。",
            )
        if claim.action != "execute" or not claim.owner_token:
            return tool_failure(
                "idempotency_claim_failed",
                "无法取得工具执行权，操作未执行。",
                retryable=True,
            )
        execution_owner_token = claim.owner_token

    handler_exception = False
    try:
        result = await tool.handler(parsed_arguments, tool_context)
    except Exception:
        handler_exception = True
        logger.exception(f"Agent tool handler failed: {tool_name}")
        normalized_result = tool_failure(
            "tool_execution_failed",
            "工具执行失败。",
        )
    else:
        normalized_result = normalize_tool_result(result)

    if execution_binding is not None:
        failure_class = classify_failure(
            normalized_result,
            handler_exception=handler_exception,
            temporary_errors=tool.idempotency_temporary_errors,
            unknown_errors=tool.idempotency_unknown_errors,
        )
        try:
            saved = await complete_execution(
                execution_binding,
                owner_token=execution_owner_token,
                result=normalized_result,
                failure_class=failure_class,
                ttl_seconds=tool.idempotency_ttl,
                temporary_failure_ttl_seconds=tool.idempotency_temporary_failure_ttl,
            )
        except Exception:
            logger.exception(f"Unable to save Agent tool execution result: {tool_name}")
        else:
            if not saved:
                logger.error(f"Agent tool execution result lost its idempotency lease: {tool_name}")

    return normalized_result
