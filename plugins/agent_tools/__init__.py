from __future__ import annotations

from .registry import (
    AgentTool,
    AgentToolNotFound,
    get_agent_tool,
    get_agent_tool_definitions,
    has_agent_tool,
    merge_agent_tool_definitions,
    register_tool,
    register_tools,
    run_registered_agent_tool,
)
from .reminder_tools import REMINDER_TOOLS


register_tools(REMINDER_TOOLS)


__all__ = [
    "AgentTool",
    "AgentToolNotFound",
    "get_agent_tool",
    "get_agent_tool_definitions",
    "has_agent_tool",
    "merge_agent_tool_definitions",
    "register_tool",
    "register_tools",
    "run_registered_agent_tool",
]
