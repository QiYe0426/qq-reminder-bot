import asyncio

from plugins import access_control, agent_tool_access
from plugins.agent_tool_access import (
    agent_tool_capabilities_state,
    group_agent_tool_state,
    is_agent_tool_allowed,
    save_group_agent_tool_state,
    set_group_agent_tool,
)


def reset_access_db(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "bot_settings.db"
    monkeypatch.setattr(access_control, "DB_PATH", db_path)
    monkeypatch.setattr(access_control, "_db_ready", False)
    monkeypatch.setattr(agent_tool_access, "ACCESS_DB_PATH", db_path)
    monkeypatch.setattr(agent_tool_access, "_db_ready", False)


def test_agent_tool_capabilities_include_admin_and_core_tools() -> None:
    tools = {item["name"]: item for item in agent_tool_capabilities_state()}

    assert tools["respond"]["configurable"] is False
    assert tools["generate_daily_report"]["requires_admin"] is True
    assert tools["get_member_profile"]["requires_group"] is False
    assert tools["set_group_features"]["requires_admin"] is True
    assert tools["web_search"]["category"] == "web"


def test_group_agent_tool_settings_default_to_enabled_and_can_be_disabled(tmp_path, monkeypatch) -> None:
    reset_access_db(tmp_path, monkeypatch)

    async def run() -> tuple[dict[str, object], tuple[bool, str], tuple[bool, str]]:
        initial = await group_agent_tool_state("1001")
        await set_group_agent_tool("1001", "web_search", False)
        disabled = await is_agent_tool_allowed("web_search", {"_target_type": "group", "_target_id": "1001"})
        await save_group_agent_tool_state("1001", {"web_search": True})
        enabled = await is_agent_tool_allowed("web_search", {"_target_type": "group", "_target_id": "1001"})
        return initial, disabled, enabled

    initial, disabled, enabled = asyncio.run(run())
    web_tool = next(item for item in initial["tools"] if item["name"] == "web_search")

    assert web_tool["enabled"] is True
    assert disabled[0] is False
    assert "联网搜索" in disabled[1]
    assert enabled == (True, "")


def test_admin_agent_tools_require_admin_context(tmp_path, monkeypatch) -> None:
    reset_access_db(tmp_path, monkeypatch)

    async def run() -> tuple[tuple[bool, str], tuple[bool, str]]:
        denied = await is_agent_tool_allowed(
            "get_group_status",
            {"_target_type": "group", "_target_id": "1001", "_is_admin": False},
        )
        allowed = await is_agent_tool_allowed(
            "get_group_status",
            {"_target_type": "group", "_target_id": "1001", "_is_admin": True},
        )
        return denied, allowed

    denied, allowed = asyncio.run(run())

    assert denied[0] is False
    assert "管理员" in denied[1]
    assert allowed == (True, "")


def test_admin_agent_tools_are_allowed_in_private_for_admin(tmp_path, monkeypatch) -> None:
    reset_access_db(tmp_path, monkeypatch)

    async def run() -> tuple[tuple[bool, str], tuple[bool, str]]:
        denied = await is_agent_tool_allowed(
            "set_group_features",
            {"_target_type": "private", "_target_id": "1261957634", "_is_admin": False},
        )
        allowed = await is_agent_tool_allowed(
            "set_group_features",
            {"_target_type": "private", "_target_id": "1261957634", "_is_admin": True},
        )
        return denied, allowed

    denied, allowed = asyncio.run(run())

    assert denied[0] is False
    assert "管理员" in denied[1]
    assert allowed == (True, "")
