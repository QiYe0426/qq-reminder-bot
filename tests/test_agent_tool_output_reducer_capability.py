from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from plugins.agent_tools.output_reducer_capability import (
    OUTPUT_REDUCER_CAPABILITIES,
    OutputReducerCapability,
    lookup_output_reducer_capability,
    resolve_enabled_output_reducer_capability,
)
from plugins.agent_tools.contracts import tool_success
from plugins.agent_tools.output_budget import apply_output_budget_framework


def _capability(*, enabled: bool = False) -> OutputReducerCapability:
    return OutputReducerCapability(
        tool_name="example",
        reducer_type="text",
        allowed_paths=(("data", "content"),),
        enabled=enabled,
        risk_level="medium",
        description="Explicit text output reduction candidate.",
    )


def test_default_capability_registry_is_empty_and_immutable() -> None:
    assert dict(OUTPUT_REDUCER_CAPABILITIES) == {}
    with pytest.raises(TypeError):
        OUTPUT_REDUCER_CAPABILITIES["example"] = _capability()  # type: ignore[index]


def test_capability_is_immutable() -> None:
    capability = _capability()

    with pytest.raises(FrozenInstanceError):
        capability.enabled = True  # type: ignore[misc]

    assert isinstance(capability.allowed_paths, tuple)
    assert all(isinstance(path, tuple) for path in capability.allowed_paths)


@pytest.mark.parametrize("path", [("error",), ("execution", "id")])
def test_protected_root_paths_are_rejected(path: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="data namespace"):
        OutputReducerCapability("example", "text", (path,))


@pytest.mark.parametrize(
    "path",
    [
        ("content",),
        ("message",),
        ("result", "text"),
    ],
)
def test_only_data_namespace_can_be_declared(path: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="data namespace"):
        OutputReducerCapability("example", "text", (path,))


def test_disabled_capability_is_visible_to_audit_but_not_runtime_candidate() -> None:
    capability = _capability(enabled=False)
    registry = {capability.tool_name: capability}

    assert lookup_output_reducer_capability("example", capabilities=registry) is capability
    assert (
        resolve_enabled_output_reducer_capability(
            "example", resolved_policy=object(), capabilities=registry
        )
        is None
    )

    original = tool_success({"content": "unchanged"})
    framework = asyncio.run(
        apply_output_budget_framework(
            tool_name="example",
            result=original,
            budget_bytes=1,
            reducers={},
        )
    )
    assert framework.status == "reducer_missing"
    assert framework.result == original


def test_missing_capability_keeps_runtime_candidate_empty() -> None:
    assert lookup_output_reducer_capability("missing") is None
    assert resolve_enabled_output_reducer_capability("missing") is None

    original = tool_success({"content": "unchanged"})
    framework = asyncio.run(
        apply_output_budget_framework(
            tool_name="missing",
            result=original,
            budget_bytes=1,
        )
    )
    assert framework.status == "reducer_missing"
    assert framework.result == original


def test_enabled_explicit_capability_can_be_resolved_without_inference() -> None:
    capability = _capability(enabled=True)

    assert (
        resolve_enabled_output_reducer_capability(
            "example", capabilities={"example": capability}
        )
        is capability
    )


def test_registry_key_must_match_declared_tool_name() -> None:
    capability = _capability(enabled=True)

    assert (
        resolve_enabled_output_reducer_capability(
            "other", capabilities={"other": capability}
        )
        is None
    )
