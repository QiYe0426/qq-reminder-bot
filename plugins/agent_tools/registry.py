from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Literal


AgentToolContext = dict[str, object]
AgentToolArguments = dict[str, object]
AgentToolResult = dict[str, object]
AgentToolHandler = Callable[[AgentToolArguments, AgentToolContext], Awaitable[AgentToolResult]]
AgentToolGroupScope = Literal["none", "current", "private_explicit"]
AgentToolSideEffect = Literal["none", "write", "external"]
AgentToolRiskLevel = Literal["low", "medium", "high"]

METADATA_RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})
METADATA_SIDE_EFFECTS = frozenset(
    {"none", "read", "database_write", "external_write", "message_send", "file_write", "mixed"}
)
METADATA_RESOURCE_SCOPES = frozenset({"none", "user", "session", "current_group", "target_group", "global"})
METADATA_CONFIRMATION_POLICIES = frozenset({"never", "optional", "required", "conditional"})
METADATA_IDEMPOTENCY_POLICIES = frozenset({"none", "single_flight", "result_cache"})


@dataclass(frozen=True)
class AgentToolMetadata:
    """Declarative Runtime policy metadata.

    Phase 1 stores and validates declarations only. The Gateway continues to
    consume the legacy AgentTool fields until the Phase 2 migration.
    """

    risk_level: str = "low"
    side_effect: str = "none"
    resource_scope: str = "none"
    confirmation_policy: str = "never"
    idempotency_policy: str = "none"
    timeout_seconds: int | None = None
    output_budget: int | None = None

    def __post_init__(self) -> None:
        allowed_values = (
            ("risk_level", self.risk_level, METADATA_RISK_LEVELS),
            ("side_effect", self.side_effect, METADATA_SIDE_EFFECTS),
            ("resource_scope", self.resource_scope, METADATA_RESOURCE_SCOPES),
            ("confirmation_policy", self.confirmation_policy, METADATA_CONFIRMATION_POLICIES),
            ("idempotency_policy", self.idempotency_policy, METADATA_IDEMPOTENCY_POLICIES),
        )
        for field_name, value, allowed in allowed_values:
            if value not in allowed:
                raise ValueError(f"Invalid AgentToolMetadata {field_name}: {value!r}")
        for field_name, value in (("timeout_seconds", self.timeout_seconds), ("output_budget", self.output_budget)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise ValueError(f"AgentToolMetadata {field_name} must be a positive integer or None.")


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
    group_scope: AgentToolGroupScope = "none"
    requires_target_group_admin: bool = False
    side_effect: AgentToolSideEffect = "none"
    risk_level: AgentToolRiskLevel = "low"
    requires_confirmation: bool = False
    confirmation_timeout: int = 120
    idempotency_enabled: bool = False
    idempotency_ttl: int = 120
    idempotency_lease_timeout: int = 60
    idempotency_temporary_failure_ttl: int = 15
    idempotency_temporary_errors: frozenset[str] = frozenset()
    idempotency_unknown_errors: frozenset[str] = frozenset()
    metadata: AgentToolMetadata = field(default_factory=AgentToolMetadata)


BUILTIN_TOOL_METADATA: dict[str, AgentToolMetadata] = {
    "web_search": AgentToolMetadata(
        risk_level="low",
        side_effect="none",
        resource_scope="none",
        confirmation_policy="never",
        idempotency_policy="none",
        timeout_seconds=15,
        output_budget=7000,
    ),
    "fetch_url": AgentToolMetadata(
        risk_level="low",
        side_effect="none",
        resource_scope="none",
        confirmation_policy="never",
        idempotency_policy="none",
        timeout_seconds=15,
        output_budget=7000,
    ),
    "get_chime": AgentToolMetadata(
        risk_level="low",
        side_effect="read",
        resource_scope="session",
        confirmation_policy="never",
    ),
    "respond": AgentToolMetadata(
        risk_level="low",
        side_effect="message_send",
        resource_scope="session",
        confirmation_policy="never",
    ),
}


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


def all_tools() -> list[AgentTool]:
    """Return all registered tools; compatibility-friendly metadata test hook."""
    return list_agent_tools()


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
    args: object,
    context: AgentToolContext,
) -> AgentToolResult:
    """Compatibility entry point routed through the tool gateway."""

    from .gateway import execute_tool

    return await execute_tool(name, args, context)
