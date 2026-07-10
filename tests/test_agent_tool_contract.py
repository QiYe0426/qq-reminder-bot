import asyncio

import pytest

from plugins.agent_tools import AgentTool, execute_tool, register_tool


def tool_definition(name: str) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Contract test tool.",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    }


def test_execute_tool_normalizes_success_and_keeps_business_error_in_data() -> None:
    async def handler(args: dict[str, object], context: dict[str, object]) -> dict[str, object]:
        return {
            "ok": True,
            "value": args["value"],
            "error": "ordinary_business_field",
            "message": "done",
        }

    name = "contract_success_test"
    register_tool(AgentTool(name=name, definition=tool_definition(name), handler=handler))

    result = asyncio.run(execute_tool(name, '{"value":"hello"}', {}))

    assert result == {
        "ok": True,
        "data": {"value": "hello", "error": "ordinary_business_field"},
        "message": "done",
    }


def test_execute_tool_returns_tool_not_found() -> None:
    result = asyncio.run(execute_tool("contract_missing_test", {}, {}))

    assert result["ok"] is False
    assert result["error"] == "tool_not_found"
    assert result["retryable"] is False


def test_execute_tool_catches_handler_exception() -> None:
    async def handler(args: dict[str, object], context: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("sensitive internal detail")

    name = "contract_exception_test"
    register_tool(AgentTool(name=name, definition=tool_definition(name), handler=handler))

    result = asyncio.run(execute_tool(name, {"value": "hello"}, {}))

    assert result == {
        "ok": False,
        "error": "tool_execution_failed",
        "message": "工具执行失败。",
        "retryable": False,
    }


def test_execute_tool_checks_access_before_handler() -> None:
    called = False

    async def handler(args: dict[str, object], context: dict[str, object]) -> dict[str, object]:
        nonlocal called
        called = True
        return {"ok": True}

    name = "contract_access_test"
    register_tool(
        AgentTool(
            name=name,
            definition=tool_definition(name),
            handler=handler,
            requires_admin=True,
        )
    )

    result = asyncio.run(execute_tool(name, {"value": "hello"}, {"_is_admin": False}))

    assert result["ok"] is False
    assert result["error"] == "tool_not_allowed"
    assert called is False


@pytest.mark.parametrize(
    "arguments",
    [
        "{bad json",
        "[]",
        {},
        {"value": 42},
        {"value": "hello", "unexpected": True},
    ],
)
def test_execute_tool_rejects_invalid_arguments(arguments: object) -> None:
    called = False

    async def handler(args: dict[str, object], context: dict[str, object]) -> dict[str, object]:
        nonlocal called
        called = True
        return {"ok": True}

    name = f"contract_arguments_test_{abs(hash(repr(arguments)))}"
    register_tool(AgentTool(name=name, definition=tool_definition(name), handler=handler))

    result = asyncio.run(execute_tool(name, arguments, {}))

    assert result["ok"] is False
    assert result["error"] == "invalid_arguments"
    assert result["retryable"] is False
    assert called is False


def test_execute_tool_preserves_explicit_business_failure_code() -> None:
    async def handler(args: dict[str, object], context: dict[str, object]) -> dict[str, object]:
        return {
            "ok": False,
            "error": "business_rule_failed",
            "message": "not allowed by the business rule",
            "reason": "closed",
        }

    name = "contract_business_failure_test"
    register_tool(AgentTool(name=name, definition=tool_definition(name), handler=handler))

    result = asyncio.run(execute_tool(name, {"value": "hello"}, {}))

    assert result == {
        "ok": False,
        "error": "business_rule_failed",
        "message": "not allowed by the business rule",
        "retryable": False,
        "data": {"reason": "closed"},
    }
