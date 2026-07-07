from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass


AgentToolContext = dict[str, object]
AgentToolArguments = dict[str, object]
AgentToolResult = dict[str, object]
AgentToolHandler = Callable[[AgentToolArguments, AgentToolContext], Awaitable[AgentToolResult]]


@dataclass(frozen=True)
class AgentTool:
    """A controlled capability that the AI agent may call."""

    name: str
    definition: dict[str, object]
    handler: AgentToolHandler
    category: str = "general"
    requires_feature: str | None = None
    requires_admin: bool = False
    requires_group: bool = False


class AgentToolNotFound(LookupError):
    """Raised when the agent asks for a tool that is not registered."""


_TOOLS: dict[str, AgentTool] = {}


def register_tool(tool: AgentTool) -> None:
    if not tool.name:
        raise ValueError("Agent tool name must not be empty.")
    _TOOLS[tool.name] = tool


def register_tools(tools: Iterable[AgentTool]) -> None:
    for tool in tools:
        register_tool(tool)


def get_agent_tool(name: str) -> AgentTool | None:
    return _TOOLS.get(name)


def has_agent_tool(name: str) -> bool:
    return name in _TOOLS


def list_agent_tools() -> list[AgentTool]:
    return list(_TOOLS.values())


def get_agent_tool_definitions(names: Iterable[str] | None = None) -> list[dict[str, object]]:
    requested = list(names) if names is not None else list(_TOOLS)
    definitions: list[dict[str, object]] = []
    for name in requested:
        tool = _TOOLS.get(name)
        if tool is not None:
            definitions.append(copy.deepcopy(tool.definition))
    return definitions


def merge_agent_tool_definitions(
    base_definitions: list[dict[str, object]],
    registered_definitions: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Replace same-name base tool definitions with registered definitions.

    This lets ai_chat.py migrate tools one family at a time without changing the
    model-facing order or breaking still-local tools.
    """

    registered_by_name = {
        str(item.get("function", {}).get("name") or ""): item
        for item in registered_definitions
        if isinstance(item.get("function"), dict)
    }
    used: set[str] = set()
    merged: list[dict[str, object]] = []

    for item in base_definitions:
        function = item.get("function")
        name = str(function.get("name") or "") if isinstance(function, dict) else ""
        replacement = registered_by_name.get(name)
        if replacement is not None:
            merged.append(copy.deepcopy(replacement))
            used.add(name)
        else:
            merged.append(item)

    for name, item in registered_by_name.items():
        if name and name not in used:
            merged.append(copy.deepcopy(item))

    return merged


async def run_registered_agent_tool(
    name: str,
    args: AgentToolArguments,
    context: AgentToolContext,
) -> AgentToolResult:
    tool = get_agent_tool(name)
    if tool is None:
        raise AgentToolNotFound(name)
    return await tool.handler(args, context)
