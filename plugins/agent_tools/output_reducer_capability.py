from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


OutputPath = tuple[str, ...]

_SUPPORTED_REDUCER_TYPES = {"text"}
_RISK_LEVELS = {"low", "medium", "high", "critical"}


@dataclass(frozen=True)
class OutputReducerCapability:
    """Explicit, non-enforcing declaration of a tool's reducible output paths."""

    tool_name: str
    reducer_type: str
    allowed_paths: tuple[OutputPath, ...]
    enabled: bool = False
    risk_level: str = "low"
    description: str = ""

    def __post_init__(self) -> None:
        if not self.tool_name or not self.tool_name.strip():
            raise ValueError("Output reducer capability requires a tool name.")
        if self.reducer_type not in _SUPPORTED_REDUCER_TYPES:
            raise ValueError("Unsupported output reducer type.")
        if not isinstance(self.enabled, bool):
            raise TypeError("Output reducer capability enabled must be boolean.")
        if self.risk_level not in _RISK_LEVELS:
            raise ValueError("Invalid output reducer capability risk level.")
        if not isinstance(self.description, str):
            raise TypeError("Output reducer capability description must be a string.")
        if not isinstance(self.allowed_paths, tuple) or not self.allowed_paths:
            raise ValueError("Output reducer capability requires explicit allowed paths.")
        if len(set(self.allowed_paths)) != len(self.allowed_paths):
            raise ValueError("Output reducer capability paths must be unique.")
        for path in self.allowed_paths:
            if not isinstance(path, tuple) or not path:
                raise TypeError("Output reducer capability paths must be non-empty tuples.")
            if any(not isinstance(part, str) or not part for part in path):
                raise TypeError("Output reducer capability path parts must be strings.")
            if path[0] != "data":
                raise ValueError("Output reducer capability paths must use data namespace.")


# Capability declarations are audit inputs only. Entries must be added explicitly;
# neither registered tools nor ToolResult values are scanned to populate this map.
OUTPUT_REDUCER_CAPABILITIES: Mapping[str, OutputReducerCapability] = MappingProxyType({})


def lookup_output_reducer_capability(
    tool_name: str,
    *,
    capabilities: Mapping[str, OutputReducerCapability] | None = None,
) -> OutputReducerCapability | None:
    """Return an explicit declaration for inventory and metadata migration work."""

    registry = capabilities if capabilities is not None else OUTPUT_REDUCER_CAPABILITIES
    capability = registry.get(tool_name)
    if capability is None or capability.tool_name != tool_name:
        return None
    return capability


def resolve_enabled_output_reducer_capability(
    tool_name: str,
    *,
    resolved_policy: object | None = None,
    capabilities: Mapping[str, OutputReducerCapability] | None = None,
) -> OutputReducerCapability | None:
    """Future policy-to-capability seam; it does not infer or enable declarations.

    ``resolved_policy`` is deliberately reserved for a future
    ``tool.metadata.output_policy`` mapping. It is not inspected in this phase.
    """

    del resolved_policy
    capability = lookup_output_reducer_capability(tool_name, capabilities=capabilities)
    if capability is None or not capability.enabled:
        return None
    return capability
