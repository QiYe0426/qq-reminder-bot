from __future__ import annotations

import pytest

from plugins.agent_tools import AgentTool, AgentToolMetadata, BUILTIN_TOOL_METADATA, all_tools


EXPECTED_METADATA: dict[str, dict[str, object]] = {
    "web_search": {
        "risk_level": "low",
        "side_effect": "none",
        "resource_scope": "none",
        "confirmation_policy": "never",
        "idempotency_policy": "none",
        "timeout_seconds": 15,
        "output_budget": 7000,
    },
    "fetch_url": {
        "risk_level": "low",
        "side_effect": "none",
        "resource_scope": "none",
        "confirmation_policy": "never",
        "idempotency_policy": "none",
        "timeout_seconds": 15,
        "output_budget": 7000,
    },
    "get_chime": {
        "risk_level": "low",
        "side_effect": "read",
        "resource_scope": "session",
        "confirmation_policy": "never",
    },
    "respond": {
        "risk_level": "low",
        "side_effect": "message_send",
        "resource_scope": "session",
        "confirmation_policy": "never",
    },
    "create_reminder": {
        "risk_level": "medium",
        "side_effect": "database_write",
        "resource_scope": "user",
        "confirmation_policy": "optional",
        "idempotency_policy": "result_cache",
    },
    "cancel_reminder": {
        "risk_level": "medium",
        "side_effect": "database_write",
        "resource_scope": "user",
        "confirmation_policy": "optional",
        "idempotency_policy": "result_cache",
    },
    "set_chime": {
        "risk_level": "high",
        "side_effect": "external_write",
        "resource_scope": "current_group",
        "confirmation_policy": "required",
        "idempotency_policy": "result_cache",
    },
    "set_group_features": {
        "risk_level": "high",
        "side_effect": "external_write",
        "resource_scope": "target_group",
        "confirmation_policy": "required",
        "idempotency_policy": "result_cache",
    },
    "generate_daily_report": {
        "risk_level": "medium",
        "side_effect": "mixed",
        "resource_scope": "target_group",
        "confirmation_policy": "required",
        "idempotency_policy": "result_cache",
        "timeout_seconds": 120,
    },
    "build_semantic_graph": {
        "risk_level": "medium",
        "side_effect": "database_write",
        "resource_scope": "target_group",
        "confirmation_policy": "required",
        "idempotency_policy": "single_flight",
    },
    "render_semantic_graph": {
        "risk_level": "high",
        "side_effect": "mixed",
        "resource_scope": "target_group",
        "confirmation_policy": "required",
        "idempotency_policy": "result_cache",
        "output_budget": 5000,
    },
}


def test_metadata_defaults() -> None:
    metadata = AgentToolMetadata()

    assert metadata.risk_level == "low"
    assert metadata.side_effect == "none"
    assert metadata.resource_scope == "none"
    assert metadata.confirmation_policy == "never"
    assert metadata.idempotency_policy == "none"
    assert metadata.timeout_seconds is None
    assert metadata.output_budget is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("risk_level", "danger"),
        ("side_effect", "network"),
        ("resource_scope", "other_group"),
        ("confirmation_policy", "always"),
        ("idempotency_policy", "forever"),
        ("timeout_seconds", 0),
        ("output_budget", -1),
    ],
)
def test_metadata_rejects_invalid_values(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        AgentToolMetadata(**{field: value})


def test_legacy_agent_tool_constructor_gets_default_metadata() -> None:
    async def handler(arguments, context):
        return {"ok": True}

    tool = AgentTool("legacy", {}, handler, "legacy_category")

    assert tool.metadata == AgentToolMetadata()
    assert tool.category == "legacy_category"


def test_all_registered_tools_declare_metadata() -> None:
    tools = all_tools()

    assert tools
    assert all(tool.metadata is not None for tool in tools)


def test_required_tool_metadata_matches_phase_one_declarations() -> None:
    metadata_by_name = {tool.name: tool.metadata for tool in all_tools()}
    metadata_by_name.update(BUILTIN_TOOL_METADATA)

    for tool_name, expected in EXPECTED_METADATA.items():
        metadata = metadata_by_name[tool_name]
        for field, value in expected.items():
            assert getattr(metadata, field) == value, f"{tool_name}.{field}"
