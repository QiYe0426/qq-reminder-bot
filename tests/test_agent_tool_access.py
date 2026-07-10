import asyncio

from plugins import access_control, agent_tool_access
from plugins.agent_tool_access import (
    agent_tool_capabilities_state,
    authorize_agent_tool,
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
    assert tools["build_semantic_graph"]["requires_feature"] == "collector"
    assert tools["render_semantic_graph"]["category"] == "semantic_graph_visual"
    assert tools["get_member_profile"]["requires_group"] is False
    assert tools["set_group_features"]["requires_admin"] is True
    assert tools["web_search"]["category"] == "web"
    assert tools["web_search"]["group_scope"] == "none"
    assert tools["get_group_status"]["group_scope"] == "private_explicit"
    assert tools["get_group_status"]["requires_target_group_admin"] is True


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


def test_target_group_scope_rejects_group_switch_and_missing_private_group(tmp_path, monkeypatch) -> None:
    reset_access_db(tmp_path, monkeypatch)

    async def run():
        switched = await authorize_agent_tool(
            "get_group_status",
            {"group_id": "2002"},
            {"_target_type": "group", "_target_id": "1001", "_user_id": "42", "_is_admin": True},
        )
        missing = await authorize_agent_tool(
            "get_group_status",
            {},
            {"_target_type": "private", "_target_id": "42", "_user_id": "42", "_is_admin": True},
        )
        return switched, missing

    switched, missing = asyncio.run(run())

    assert switched.allowed is False
    assert switched.error == "group_permission_denied"
    assert missing.allowed is False
    assert missing.error == "missing_group_id"


def test_private_target_group_checks_role_tool_setting_and_feature(tmp_path, monkeypatch) -> None:
    reset_access_db(tmp_path, monkeypatch)
    checked_roles: list[tuple[str, str]] = []
    checked_tools: list[tuple[str, str]] = []
    checked_features: list[tuple[str, str]] = []

    async def target_group_admin_authorized(user_id: str, group_id: str, context: dict[str, object]) -> bool:
        checked_roles.append((user_id, group_id))
        return group_id != "9000"

    async def tool_enabled(group_id: str, tool_name: str) -> bool:
        checked_tools.append((group_id, tool_name))
        return group_id != "9001"

    async def feature_enabled(group_id: str, feature: str) -> bool:
        checked_features.append((group_id, feature))
        return group_id != "9002"

    monkeypatch.setattr(agent_tool_access, "target_group_admin_authorized", target_group_admin_authorized)
    monkeypatch.setattr(agent_tool_access, "agent_tool_enabled_for_group", tool_enabled)
    monkeypatch.setattr(agent_tool_access, "is_group_feature_enabled", feature_enabled)

    async def run():
        context = {"_target_type": "private", "_target_id": "42", "_user_id": "42", "_is_admin": True}
        denied_role = await authorize_agent_tool("get_group_status", {"group_id": "9000"}, context)
        denied_tool = await authorize_agent_tool("get_group_status", {"group_id": "9001"}, context)
        denied_feature = await authorize_agent_tool("get_group_status", {"group_id": "9002"}, context)
        allowed = await authorize_agent_tool("get_group_status", {"group_id": "9003"}, context)
        return denied_role, denied_tool, denied_feature, allowed

    denied_role, denied_tool, denied_feature, allowed = asyncio.run(run())

    assert denied_role.error == "group_permission_denied"
    assert denied_tool.error == "tool_not_allowed"
    assert denied_feature.error == "feature_disabled"
    assert allowed.allowed is True
    assert allowed.effective_group_id == "9003"
    assert ("42", "9003") in checked_roles
    assert ("9003", "get_group_status") in checked_tools
    assert ("9003", "ai_chat") in checked_features
