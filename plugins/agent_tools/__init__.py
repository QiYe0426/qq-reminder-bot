from __future__ import annotations

from .registry import (
    AgentTool,
    AgentToolMetadata,
    AgentToolNotFound,
    BUILTIN_TOOL_METADATA,
    all_tools,
    get_agent_tool,
    get_agent_tool_definitions,
    has_agent_tool,
    list_agent_tools,
    merge_agent_tool_definitions,
    register_tool,
    register_tools,
    run_registered_agent_tool,
)
from .gateway import execute_tool
from .chime_tools import CHIME_TOOLS
from .group_context_tools import GROUP_CONTEXT_TOOLS
from .knowledge_tools import KNOWLEDGE_TOOLS
from .reminder_tools import REMINDER_TOOLS
from .semantic_graph_tools import SEMANTIC_GRAPH_TOOLS
from .admin_tools import ADMIN_TOOLS


register_tools(REMINDER_TOOLS)
register_tools(CHIME_TOOLS)
register_tools(KNOWLEDGE_TOOLS)
register_tools(GROUP_CONTEXT_TOOLS)
register_tools(SEMANTIC_GRAPH_TOOLS)
register_tools(ADMIN_TOOLS)


__all__ = [
    "AgentTool",
    "AgentToolMetadata",
    "AgentToolNotFound",
    "BUILTIN_TOOL_METADATA",
    "all_tools",
    "get_agent_tool",
    "get_agent_tool_definitions",
    "has_agent_tool",
    "list_agent_tools",
    "merge_agent_tool_definitions",
    "register_tool",
    "register_tools",
    "run_registered_agent_tool",
    "execute_tool",
]
