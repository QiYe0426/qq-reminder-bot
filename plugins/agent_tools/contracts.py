from __future__ import annotations

import json
import re
from collections.abc import Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


ToolArguments = dict[str, object]
ToolResult = dict[str, object]

_ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def tool_success(data: Mapping[str, object] | None = None, message: str = "") -> ToolResult:
    return {
        "ok": True,
        "data": dict(data or {}),
        "message": str(message or ""),
    }


def tool_failure(
    error: str,
    message: str,
    *,
    retryable: bool = False,
    data: Mapping[str, object] | None = None,
) -> ToolResult:
    result: ToolResult = {
        "ok": False,
        "error": error,
        "message": str(message or ""),
        "retryable": bool(retryable),
    }
    if data:
        result["data"] = dict(data)
    return result


def parse_tool_arguments(arguments: object) -> tuple[ToolArguments | None, ToolResult | None]:
    if isinstance(arguments, dict):
        return dict(arguments), None
    if not isinstance(arguments, str) or not arguments.strip():
        return None, tool_failure(
            "invalid_arguments",
            "工具参数必须是 JSON 对象。",
        )
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return None, tool_failure(
            "invalid_arguments",
            "工具参数不是有效的 JSON。",
        )
    if not isinstance(parsed, dict):
        return None, tool_failure(
            "invalid_arguments",
            "工具参数必须是 JSON 对象。",
        )
    return parsed, None


def validate_tool_arguments(arguments: ToolArguments, parameters: object) -> ToolResult | None:
    if not isinstance(parameters, dict):
        return tool_failure(
            "invalid_tool_definition",
            "工具参数定义无效。",
        )
    try:
        Draft202012Validator.check_schema(parameters)
        validator = Draft202012Validator(parameters)
        errors = sorted(validator.iter_errors(arguments), key=lambda item: list(item.absolute_path))
    except SchemaError:
        return tool_failure(
            "invalid_tool_definition",
            "工具参数定义无效。",
        )
    if not errors:
        return None

    error = errors[0]
    field = ".".join(str(item) for item in error.absolute_path)
    message = f"工具参数不符合定义：{error.message}"
    if field:
        message = f"工具参数 {field} 不符合定义：{error.message}"
    return tool_failure("invalid_arguments", message)


def normalize_tool_result(result: object) -> ToolResult:
    if not isinstance(result, dict):
        return tool_failure(
            "invalid_tool_result",
            "工具返回了无效结果。",
        )

    message = str(result.get("message") or "")
    if result.get("ok") is False:
        raw_error = result.get("error")
        error = str(raw_error or "tool_failed").strip()
        data = {
            key: value
            for key, value in result.items()
            if key not in {"ok", "error", "message", "retryable"}
        }
        if not _ERROR_CODE_PATTERN.fullmatch(error):
            if raw_error not in (None, ""):
                data["handler_error"] = raw_error
            error = "tool_failed"
        return tool_failure(
            error,
            message,
            retryable=result.get("retryable") is True,
            data=data,
        )

    data = {key: value for key, value in result.items() if key not in {"ok", "message"}}
    return tool_success(data, message)
