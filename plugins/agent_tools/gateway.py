from __future__ import annotations

import asyncio
import time

from nonebot.log import logger

from . import audit
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
    FAILURE_TEMPORARY,
    classify_failure,
    claim_execution,
    complete_execution,
    create_execution_binding,
)
from .policy import resolve_agent_tool_policy
from .output_budget import (
    OUTPUT_BUDGET_REDUCERS,
    apply_output_budget_framework,
    output_budget_enforcement_enabled,
    serialized_tool_result_size_bytes,
)
from .output_budget_shadow_reduction import (
    OUTPUT_BUDGET_SHADOW_REDUCERS,
    evaluate_output_budget_shadow_reduction,
)
from .authorization_policy import resolve_authorization_policy
from .authorization_shadow import (
    authorization_v2_enforcement_enabled,
    compare_authorization_decisions,
    evaluate_metadata_authorization,
    log_authorization_shadow_comparison,
)
from .registry import get_agent_tool


def tool_parameters(definition: dict[str, object]) -> object:
    function = definition.get("function")
    if not isinstance(function, dict):
        return None
    return function.get("parameters")


def _duration_ms(started_at: float) -> int:
    return max(0, int((time.monotonic() - started_at) * 1000))


def _arguments_fingerprint(arguments: object) -> str:
    if not isinstance(arguments, dict):
        return ""
    try:
        return audit.fingerprint_arguments(arguments)
    except Exception:
        logger.exception("Unable to fingerprint Agent tool arguments for audit")
        return ""


def _serialized_tool_result_size_bytes(result: ToolResult) -> int:
    return serialized_tool_result_size_bytes(result)


def _record_output_budget_shadow(
    *,
    tool_name: str,
    size_bytes: int,
    budget_bytes: int,
    exceeded: bool,
) -> None:
    logger.info(
        "Agent tool output budget shadow: tool_name={} size_bytes={} budget_bytes={} exceeded={}",
        tool_name,
        size_bytes,
        budget_bytes,
        str(exceeded).lower(),
    )


def _observe_output_budget_shadow(
    *,
    tool_name: str,
    result: ToolResult,
    output_budget: int | None,
) -> int | None:
    if output_budget is None:
        return None
    try:
        size_bytes = _serialized_tool_result_size_bytes(result)
        _record_output_budget_shadow(
            tool_name=tool_name,
            size_bytes=size_bytes,
            budget_bytes=output_budget,
            exceeded=size_bytes > output_budget,
        )
        return size_bytes
    except Exception:
        # Never include the result or exception text: either may contain output.
        logger.warning("Agent tool output budget shadow failed: tool_name=%s", tool_name)
        return None


def _record_output_budget_framework(
    *,
    tool_name: str,
    status: str,
    budget_bytes: int,
    before_size_bytes: int,
    after_size_bytes: int,
) -> None:
    try:
        logger.info(
            "Agent tool output budget framework: tool_name=%s status=%s budget_bytes=%d "
            "before_size_bytes=%d after_size_bytes=%d",
            tool_name,
            status,
            budget_bytes,
            before_size_bytes,
            after_size_bytes,
        )
    except Exception:
        # Telemetry must never affect the ToolResult or include output content.
        return


def _record_output_budget_shadow_reduction(
    *,
    tool_name: str,
    reducer_type: str,
    before_size_bytes: int,
    after_size_bytes: int,
    reduction_ratio: float,
    status: str,
) -> None:
    logger.info(
        "Agent tool output budget shadow reduction: tool_name={} reducer_type={} "
        "before_size_bytes={} after_size_bytes={} reduction_ratio={:.6f} status={}",
        tool_name,
        reducer_type,
        before_size_bytes,
        after_size_bytes,
        reduction_ratio,
        status,
    )


async def _observe_output_budget_shadow_reduction(
    *,
    tool_name: str,
    result: ToolResult,
    output_budget: int,
    before_size_bytes: int,
) -> None:
    try:
        observation = await evaluate_output_budget_shadow_reduction(
            tool_name=tool_name,
            result=result,
            budget_bytes=output_budget,
            before_size_bytes=before_size_bytes,
            reducers=OUTPUT_BUDGET_SHADOW_REDUCERS,
        )
        if observation.status == "not_allowed":
            return
        _record_output_budget_shadow_reduction(
            tool_name=tool_name,
            reducer_type=observation.reducer_type,
            before_size_bytes=observation.before_size_bytes,
            after_size_bytes=observation.after_size_bytes,
            reduction_ratio=observation.reduction_ratio,
            status=observation.status,
        )
    except Exception:
        # Never log result content or exception text from shadow evaluation.
        logger.warning(
            "Agent tool output budget shadow reduction failed: tool_name=%s",
            tool_name,
        )


async def _append_audit_event(
    *,
    invocation_id: str,
    event_type: str,
    tool_name: str,
    context: dict[str, object],
    risk_level: str = "",
    side_effect: str = "",
    effective_group_id: str = "",
    arguments_fingerprint: str = "",
    confirmation_id: int | None = None,
    confirmation_status: str = "",
    idempotency_key: str = "",
    idempotency_status: str = "",
    execution_stage: str = "",
    outcome: str = "",
    error_code: str = "",
    retryable: bool = False,
    failure_class: str = "",
    duration_ms: int = 0,
) -> bool:
    try:
        await audit.append_event(
            invocation_id=invocation_id,
            event_type=event_type,
            tool_call_id=str(context.get("_tool_call_id") or ""),
            tool_name=tool_name,
            invocation_source=str(context.get("_invocation_source") or "agent"),
            actor_user_id=str(context.get("_user_id") or ""),
            session_target_type=str(context.get("_target_type") or ""),
            session_target_id=str(context.get("_target_id") or ""),
            effective_group_id=effective_group_id,
            arguments_fingerprint=arguments_fingerprint,
            confirmation_id=confirmation_id,
            confirmation_status=confirmation_status,
            idempotency_key=idempotency_key,
            idempotency_status=idempotency_status,
            risk_level=risk_level,
            side_effect=side_effect,
            execution_stage=execution_stage,
            outcome=outcome,
            error_code=error_code,
            retryable=retryable,
            failure_class=failure_class,
            duration_ms=duration_ms,
        )
    except Exception:
        logger.exception(f"Unable to append Agent tool audit event: {event_type} ({tool_name})")
        return False
    return True


async def execute_tool(tool_name: str, arguments: object, context: dict[str, object]) -> ToolResult:
    started_at = time.monotonic()
    invocation_id = audit.create_invocation_id()
    tool = get_agent_tool(tool_name)
    resolved_policy = resolve_agent_tool_policy(tool) if tool is not None else None
    risk_level = resolved_policy.risk_level if resolved_policy is not None else ""
    side_effect = resolved_policy.side_effect if resolved_policy is not None else ""
    arguments_fingerprint = _arguments_fingerprint(arguments)
    await _append_audit_event(
        invocation_id=invocation_id,
        event_type="tool_requested",
        tool_name=tool_name,
        context=context,
        risk_level=risk_level,
        side_effect=side_effect,
        arguments_fingerprint=arguments_fingerprint,
        execution_stage="requested",
        outcome="pending",
    )
    if tool is None:
        result = tool_failure(
            "tool_not_found",
            f"未知 Agent 工具：{tool_name}",
        )
        await _append_audit_event(
            invocation_id=invocation_id,
            event_type="execution_failed",
            tool_name=tool_name,
            context=context,
            arguments_fingerprint=arguments_fingerprint,
            execution_stage="lookup",
            outcome="failure",
            error_code="tool_not_found",
            failure_class="permanent",
            duration_ms=_duration_ms(started_at),
        )
        return result

    parsed_arguments, parse_error = parse_tool_arguments(arguments)
    if parse_error is not None:
        await _append_audit_event(
            invocation_id=invocation_id,
            event_type="validation_failed",
            tool_name=tool_name,
            context=context,
            risk_level=risk_level,
            side_effect=side_effect,
            arguments_fingerprint=arguments_fingerprint,
            execution_stage="validation",
            outcome="failure",
            error_code=str(parse_error.get("error") or "invalid_arguments"),
            retryable=parse_error.get("retryable") is True,
            failure_class="permanent",
            duration_ms=_duration_ms(started_at),
        )
        return parse_error
    assert parsed_arguments is not None
    arguments_fingerprint = _arguments_fingerprint(parsed_arguments)

    validation_error = validate_tool_arguments(parsed_arguments, tool_parameters(tool.definition))
    if validation_error is not None:
        await _append_audit_event(
            invocation_id=invocation_id,
            event_type="validation_failed",
            tool_name=tool_name,
            context=context,
            risk_level=risk_level,
            side_effect=side_effect,
            arguments_fingerprint=arguments_fingerprint,
            execution_stage="validation",
            outcome="failure",
            error_code=str(validation_error.get("error") or "invalid_arguments"),
            retryable=validation_error.get("retryable") is True,
            failure_class="permanent",
            duration_ms=_duration_ms(started_at),
        )
        return validation_error

    # Imported lazily to keep registry metadata independent from the access layer.
    from plugins.agent_tool_access import authorize_agent_tool

    authorization = await authorize_agent_tool(tool_name, parsed_arguments, context)
    metadata_authorization = None
    try:
        authorization_shadow_policy = resolve_authorization_policy(tool)
        metadata_authorization = await evaluate_metadata_authorization(
            tool_name=tool_name,
            arguments=parsed_arguments,
            context=context,
            policy=authorization_shadow_policy,
        )
        authorization_comparison = compare_authorization_decisions(
            legacy_allowed=authorization.allowed,
            metadata_result=metadata_authorization,
            policy=authorization_shadow_policy,
        )
        log_authorization_shadow_comparison(
            invocation_id=invocation_id,
            tool_name=tool_name,
            comparison=authorization_comparison,
        )
    except Exception:
        logger.exception(
            "Agent authorization shadow setup failed: invocation_id=%s tool_name=%s",
            invocation_id,
            tool_name,
        )
    if not authorization.allowed:
        result = tool_failure(
            authorization.error or "tool_not_allowed",
            authorization.message,
        )
        await _append_audit_event(
            invocation_id=invocation_id,
            event_type="authorization_denied",
            tool_name=tool_name,
            context=context,
            risk_level=risk_level,
            side_effect=side_effect,
            arguments_fingerprint=arguments_fingerprint,
            effective_group_id=str(authorization.effective_group_id or ""),
            execution_stage="authorization",
            outcome="failure",
            error_code=str(result.get("error") or "tool_not_allowed"),
            failure_class="permanent",
            duration_ms=_duration_ms(started_at),
        )
        return result

    if authorization_v2_enforcement_enabled() and (
        metadata_authorization is None or not metadata_authorization.allowed
    ):
        result = tool_failure(
            "tool_not_allowed",
            "工具授权未通过。",
        )
        await _append_audit_event(
            invocation_id=invocation_id,
            event_type="authorization_denied",
            tool_name=tool_name,
            context=context,
            risk_level=risk_level,
            side_effect=side_effect,
            arguments_fingerprint=arguments_fingerprint,
            effective_group_id=(
                metadata_authorization.effective_group_id
                if metadata_authorization is not None
                else ""
            ),
            execution_stage="authorization",
            outcome="failure",
            error_code="tool_not_allowed",
            failure_class="permanent",
            duration_ms=_duration_ms(started_at),
        )
        return result

    tool_context = dict(context)
    if authorization.effective_group_id:
        tool_context["_effective_group_id"] = authorization.effective_group_id

    audit_confirmation_id: int | None = None
    audit_confirmation_status = ""
    if resolved_policy.requires_confirmation:
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
                result = tool_failure(
                    "confirmation_state_failed",
                    "无法创建工具确认请求。",
                )
                await _append_audit_event(
                    invocation_id=invocation_id,
                    event_type="execution_failed",
                    tool_name=tool_name,
                    context=tool_context,
                    risk_level=risk_level,
                    side_effect=side_effect,
                    effective_group_id=str(tool_context.get("_effective_group_id") or ""),
                    arguments_fingerprint=arguments_fingerprint,
                    execution_stage="confirmation",
                    outcome="failure",
                    error_code="confirmation_state_failed",
                    failure_class="unknown",
                    duration_ms=_duration_ms(started_at),
                )
                return result
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
            result = tool_failure(
                "confirmation_required",
                message,
                data={
                    "confirmation_code": confirmation.confirmation_code,
                    "tool_name": tool_name,
                    "arguments_hash": confirmation.arguments_hash,
                    "expires_at": confirmation.expires_at,
                },
            )
            await _append_audit_event(
                invocation_id=invocation_id,
                event_type="confirmation_required",
                tool_name=tool_name,
                context=tool_context,
                risk_level=risk_level,
                side_effect=side_effect,
                effective_group_id=str(tool_context.get("_effective_group_id") or ""),
                arguments_fingerprint=arguments_fingerprint,
                confirmation_id=confirmation.confirmation_id,
                confirmation_status=confirmation.status,
                execution_stage="confirmation",
                outcome="pending",
                error_code="confirmation_required",
                duration_ms=_duration_ms(started_at),
            )
            return result

        validation = await validate_confirmation(confirmation_token, **confirmation_binding)
        if not validation.ok:
            result = tool_failure(validation.error or "confirmation_invalid", validation.message)
            await _append_audit_event(
                invocation_id=invocation_id,
                event_type="execution_failed",
                tool_name=tool_name,
                context=tool_context,
                risk_level=risk_level,
                side_effect=side_effect,
                effective_group_id=str(tool_context.get("_effective_group_id") or ""),
                arguments_fingerprint=arguments_fingerprint,
                confirmation_id=(
                    validation.confirmation.confirmation_id if validation.confirmation is not None else None
                ),
                confirmation_status=(
                    validation.confirmation.status if validation.confirmation is not None else ""
                ),
                execution_stage="confirmation",
                outcome="failure",
                error_code=str(result.get("error") or "confirmation_invalid"),
                failure_class="permanent",
                duration_ms=_duration_ms(started_at),
            )
            return result
        consumption = await consume_confirmation(confirmation_token, **confirmation_binding)
        if not consumption.ok:
            result = tool_failure(consumption.error or "confirmation_invalid", consumption.message)
            await _append_audit_event(
                invocation_id=invocation_id,
                event_type="execution_failed",
                tool_name=tool_name,
                context=tool_context,
                risk_level=risk_level,
                side_effect=side_effect,
                effective_group_id=str(tool_context.get("_effective_group_id") or ""),
                arguments_fingerprint=arguments_fingerprint,
                confirmation_id=(
                    consumption.confirmation.confirmation_id if consumption.confirmation is not None else None
                ),
                confirmation_status=(
                    consumption.confirmation.status if consumption.confirmation is not None else ""
                ),
                execution_stage="confirmation",
                outcome="failure",
                error_code=str(result.get("error") or "confirmation_invalid"),
                failure_class="permanent",
                duration_ms=_duration_ms(started_at),
            )
            return result
        confirmed = consumption.confirmation
        audit_confirmation_id = confirmed.confirmation_id if confirmed is not None else None
        audit_confirmation_status = confirmed.status if confirmed is not None else "consumed"
        await _append_audit_event(
            invocation_id=invocation_id,
            event_type="confirmation_accepted",
            tool_name=tool_name,
            context=tool_context,
            risk_level=risk_level,
            side_effect=side_effect,
            effective_group_id=str(tool_context.get("_effective_group_id") or ""),
            arguments_fingerprint=arguments_fingerprint,
            confirmation_id=audit_confirmation_id,
            confirmation_status=audit_confirmation_status,
            execution_stage="confirmation",
            outcome="success",
        )
        tool_context.pop("_tool_confirmation_token", None)

    execution_binding = None
    execution_owner_token = ""
    if resolved_policy.idempotency_enabled:
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
            result = tool_failure(
                "idempotency_unavailable",
                "无法建立工具重复执行保护，操作未执行。",
                retryable=True,
            )
            await _append_audit_event(
                invocation_id=invocation_id,
                event_type="execution_failed",
                tool_name=tool_name,
                context=tool_context,
                risk_level=risk_level,
                side_effect=side_effect,
                effective_group_id=str(tool_context.get("_effective_group_id") or ""),
                arguments_fingerprint=arguments_fingerprint,
                idempotency_key=execution_binding.idempotency_key,
                idempotency_status="unavailable",
                execution_stage="idempotency",
                outcome="failure",
                error_code="idempotency_unavailable",
                retryable=True,
                failure_class="temporary",
                duration_ms=_duration_ms(started_at),
            )
            return result
        if claim.action == "cached" and claim.record is not None and claim.record.result is not None:
            await _append_audit_event(
                invocation_id=invocation_id,
                event_type="idempotency_replayed",
                tool_name=tool_name,
                context=tool_context,
                risk_level=risk_level,
                side_effect=side_effect,
                effective_group_id=str(tool_context.get("_effective_group_id") or ""),
                arguments_fingerprint=arguments_fingerprint,
                idempotency_key=execution_binding.idempotency_key,
                idempotency_status=claim.record.status,
                execution_stage="idempotency",
                outcome="replayed",
                error_code=str(claim.record.result.get("error") or ""),
                retryable=claim.record.result.get("retryable") is True,
                failure_class=claim.record.failure_class,
                duration_ms=_duration_ms(started_at),
            )
            return claim.record.result
        if claim.action == "running":
            result = tool_failure(
                "already_running",
                "相同工具调用正在执行，请稍后查看结果。",
                retryable=True,
            )
            await _append_audit_event(
                invocation_id=invocation_id,
                event_type="idempotency_running",
                tool_name=tool_name,
                context=tool_context,
                risk_level=risk_level,
                side_effect=side_effect,
                effective_group_id=str(tool_context.get("_effective_group_id") or ""),
                arguments_fingerprint=arguments_fingerprint,
                idempotency_key=execution_binding.idempotency_key,
                idempotency_status="running",
                execution_stage="idempotency",
                outcome="blocked",
                error_code="already_running",
                retryable=True,
                failure_class="temporary",
                duration_ms=_duration_ms(started_at),
            )
            return result
        if claim.action == "unknown":
            result = tool_failure(
                "execution_state_unknown",
                "相同工具调用的执行状态无法确定，为避免重复副作用，未再次执行。",
            )
            await _append_audit_event(
                invocation_id=invocation_id,
                event_type="idempotency_unknown",
                tool_name=tool_name,
                context=tool_context,
                risk_level=risk_level,
                side_effect=side_effect,
                effective_group_id=str(tool_context.get("_effective_group_id") or ""),
                arguments_fingerprint=arguments_fingerprint,
                idempotency_key=execution_binding.idempotency_key,
                idempotency_status="unknown",
                execution_stage="idempotency",
                outcome="blocked",
                error_code="execution_state_unknown",
                failure_class="unknown",
                duration_ms=_duration_ms(started_at),
            )
            return result
        if claim.action != "execute" or not claim.owner_token:
            result = tool_failure(
                "idempotency_claim_failed",
                "无法取得工具执行权，操作未执行。",
                retryable=True,
            )
            await _append_audit_event(
                invocation_id=invocation_id,
                event_type="execution_failed",
                tool_name=tool_name,
                context=tool_context,
                risk_level=risk_level,
                side_effect=side_effect,
                effective_group_id=str(tool_context.get("_effective_group_id") or ""),
                arguments_fingerprint=arguments_fingerprint,
                idempotency_key=execution_binding.idempotency_key,
                idempotency_status="claim_failed",
                execution_stage="idempotency",
                outcome="failure",
                error_code="idempotency_claim_failed",
                retryable=True,
                failure_class="temporary",
                duration_ms=_duration_ms(started_at),
            )
            return result
        execution_owner_token = claim.owner_token
        await _append_audit_event(
            invocation_id=invocation_id,
            event_type="idempotency_claimed",
            tool_name=tool_name,
            context=tool_context,
            risk_level=risk_level,
            side_effect=side_effect,
            effective_group_id=str(tool_context.get("_effective_group_id") or ""),
            arguments_fingerprint=arguments_fingerprint,
            idempotency_key=execution_binding.idempotency_key,
            idempotency_status="running",
            execution_stage="idempotency",
            outcome="success",
        )

    execution_started_logged = await _append_audit_event(
        invocation_id=invocation_id,
        event_type="execution_started",
        tool_name=tool_name,
        context=tool_context,
        risk_level=risk_level,
        side_effect=side_effect,
        effective_group_id=str(tool_context.get("_effective_group_id") or ""),
        arguments_fingerprint=arguments_fingerprint,
        confirmation_id=audit_confirmation_id,
        confirmation_status=audit_confirmation_status,
        idempotency_key=execution_binding.idempotency_key if execution_binding is not None else "",
        idempotency_status="running" if execution_binding is not None else "",
        execution_stage="handler",
        outcome="pending",
    )
    if not execution_started_logged and risk_level == "high":
        normalized_result = tool_failure(
            "audit_unavailable",
            "Audit logging is unavailable; the high-risk operation was not executed.",
            retryable=True,
        )
        if execution_binding is not None:
            try:
                await complete_execution(
                    execution_binding,
                    owner_token=execution_owner_token,
                    result=normalized_result,
                    failure_class=FAILURE_TEMPORARY,
                    ttl_seconds=tool.idempotency_ttl,
                    temporary_failure_ttl_seconds=tool.idempotency_temporary_failure_ttl,
                )
            except Exception:
                logger.exception(f"Unable to close blocked Agent tool execution: {tool_name}")
        await _append_audit_event(
            invocation_id=invocation_id,
            event_type="execution_failed",
            tool_name=tool_name,
            context=tool_context,
            risk_level=risk_level,
            side_effect=side_effect,
            effective_group_id=str(tool_context.get("_effective_group_id") or ""),
            arguments_fingerprint=arguments_fingerprint,
            confirmation_id=audit_confirmation_id,
            confirmation_status=audit_confirmation_status,
            idempotency_key=execution_binding.idempotency_key if execution_binding is not None else "",
            idempotency_status="failed" if execution_binding is not None else "",
            execution_stage="audit",
            outcome="failure",
            error_code="audit_unavailable",
            retryable=True,
            failure_class=FAILURE_TEMPORARY,
            duration_ms=_duration_ms(started_at),
        )
        return normalized_result

    handler_exception = False
    handler_timed_out = False
    try:
        if resolved_policy.timeout_seconds is None:
            result = await tool.handler(parsed_arguments, tool_context)
        else:
            result = await asyncio.wait_for(
                tool.handler(parsed_arguments, tool_context),
                timeout=resolved_policy.timeout_seconds,
            )
    except asyncio.TimeoutError:
        handler_timed_out = True
        logger.warning(f"Agent tool handler timed out: {tool_name}")
        normalized_result = tool_failure(
            "tool_execution_timeout",
            "工具执行超时，执行结果无法确认。",
        )
    except Exception:
        handler_exception = True
        logger.exception(f"Agent tool handler failed: {tool_name}")
        normalized_result = tool_failure(
            "tool_execution_failed",
            "工具执行失败。",
        )
    else:
        normalized_result = normalize_tool_result(result)

    output_size_bytes = _observe_output_budget_shadow(
        tool_name=tool_name,
        result=normalized_result,
        output_budget=resolved_policy.output_budget,
    )
    if (
        resolved_policy.output_budget is not None
        and output_size_bytes is not None
        and output_size_bytes > resolved_policy.output_budget
    ):
        await _observe_output_budget_shadow_reduction(
            tool_name=tool_name,
            result=normalized_result,
            output_budget=resolved_policy.output_budget,
            before_size_bytes=output_size_bytes,
        )
    if (
        resolved_policy.output_budget is not None
        and output_size_bytes is not None
        and output_size_bytes > resolved_policy.output_budget
        and output_budget_enforcement_enabled()
    ):
        framework_result = await apply_output_budget_framework(
            tool_name=tool_name,
            result=normalized_result,
            budget_bytes=resolved_policy.output_budget,
            reducers=OUTPUT_BUDGET_REDUCERS,
        )
        final_size_bytes = output_size_bytes
        if framework_result.status == "reduced":
            normalized_result = framework_result.result
            measured_size = _observe_output_budget_shadow(
                tool_name=tool_name,
                result=normalized_result,
                output_budget=resolved_policy.output_budget,
            )
            if measured_size is not None:
                final_size_bytes = measured_size
        _record_output_budget_framework(
            tool_name=tool_name,
            status=framework_result.status,
            budget_bytes=resolved_policy.output_budget,
            before_size_bytes=output_size_bytes,
            after_size_bytes=final_size_bytes,
        )

    failure_class = classify_failure(
        normalized_result,
        handler_exception=handler_exception or handler_timed_out,
        temporary_errors=tool.idempotency_temporary_errors,
        unknown_errors=tool.idempotency_unknown_errors,
    )
    idempotency_status = ""
    if execution_binding is not None:
        if failure_class == "unknown":
            idempotency_status = "unknown"
        elif normalized_result.get("ok") is True:
            idempotency_status = "succeeded"
        else:
            idempotency_status = "failed"
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

    succeeded = normalized_result.get("ok") is True
    await _append_audit_event(
        invocation_id=invocation_id,
        event_type="execution_completed" if succeeded else "execution_failed",
        tool_name=tool_name,
        context=tool_context,
        risk_level=risk_level,
        side_effect=side_effect,
        effective_group_id=str(tool_context.get("_effective_group_id") or ""),
        arguments_fingerprint=arguments_fingerprint,
        confirmation_id=audit_confirmation_id,
        confirmation_status=audit_confirmation_status,
        idempotency_key=execution_binding.idempotency_key if execution_binding is not None else "",
        idempotency_status=idempotency_status,
        execution_stage="handler",
        outcome="success" if succeeded else "failure",
        error_code=str(normalized_result.get("error") or ""),
        retryable=normalized_result.get("retryable") is True,
        failure_class=failure_class,
        duration_ms=_duration_ms(started_at),
    )
    return normalized_result
