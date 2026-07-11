from __future__ import annotations

import copy
from dataclasses import dataclass

from ..contracts import ToolResult
from ..output_budget import (
    canonicalize_output_budget_result,
    serialized_tool_result_size_bytes,
)


TextPath = tuple[str, ...]
_FORBIDDEN_PATH_PARTS = {
    "ok",
    "error",
    "retryable",
    "message",
    "execution",
    "idempotency",
    "confirmation",
}


def _path_value(result: ToolResult, path: TextPath) -> object:
    current: object = result
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _set_path_value(result: ToolResult, path: TextPath, value: str) -> bool:
    current: object = result
    for part in path[:-1]:
        if not isinstance(current, dict) or not isinstance(current.get(part), dict):
            return False
        current = current[part]
    if not isinstance(current, dict) or path[-1] not in current:
        return False
    current[path[-1]] = value
    return True


@dataclass(frozen=True)
class TextReducer:
    """Deterministically reduce only explicitly whitelisted text paths.

    Paths are ordered from highest to lowest preservation priority. Reduction
    therefore processes paths in reverse order.
    """

    text_paths: tuple[TextPath, ...]
    marker: str = "…"

    def __post_init__(self) -> None:
        if not self.text_paths:
            raise ValueError("TextReducer requires at least one text path.")
        if len(set(self.text_paths)) != len(self.text_paths):
            raise ValueError("TextReducer text paths must be unique.")
        if not isinstance(self.marker, str):
            raise TypeError("TextReducer marker must be a string.")
        for path in self.text_paths:
            if not path or path[0] != "data":
                raise ValueError("TextReducer paths must start at data.")
            if any(part.lower() in _FORBIDDEN_PATH_PARTS for part in path):
                raise ValueError("TextReducer path targets a protected field.")

    async def reduce(self, result: ToolResult, budget_bytes: int) -> ToolResult:
        canonical = canonicalize_output_budget_result(result)
        if canonical is None:
            return copy.deepcopy(result)
        original = copy.deepcopy(canonical)
        if budget_bytes <= 0 or serialized_tool_result_size_bytes(canonical) <= budget_bytes:
            return canonical

        working = copy.deepcopy(canonical)
        for path in reversed(self.text_paths):
            if serialized_tool_result_size_bytes(working) <= budget_bytes:
                break
            text = _path_value(working, path)
            if not isinstance(text, str) or not text:
                continue

            marker_candidate = copy.deepcopy(working)
            _set_path_value(marker_candidate, path, self.marker)
            if serialized_tool_result_size_bytes(marker_candidate) <= budget_bytes:
                low = 0
                high = max(0, len(text) - 1)
                best = self.marker
                while low <= high:
                    middle = (low + high) // 2
                    candidate_text = text[:middle] + self.marker
                    candidate = copy.deepcopy(working)
                    _set_path_value(candidate, path, candidate_text)
                    if serialized_tool_result_size_bytes(candidate) <= budget_bytes:
                        best = candidate_text
                        low = middle + 1
                    else:
                        high = middle - 1
                _set_path_value(working, path, best)
            else:
                _set_path_value(working, path, "")

        if serialized_tool_result_size_bytes(working) > budget_bytes:
            return original
        rebuilt = canonicalize_output_budget_result(working)
        return rebuilt if rebuilt is not None else original
