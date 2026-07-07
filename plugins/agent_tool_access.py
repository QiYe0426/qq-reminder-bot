from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime

import aiosqlite

from plugins.access_control import DB_PATH as ACCESS_DB_PATH, init_access_db, is_group_feature_enabled
from plugins.agent_tools import get_agent_tool, list_agent_tools


@dataclass(frozen=True)
class AgentToolCapability:
    name: str
    label: str
    description: str
    category: str
    source: str
    requires_feature: str | None = None
    requires_admin: bool = False
    requires_group: bool = False
    configurable: bool = True
    default_enabled: bool = True


BUILTIN_TOOL_LABELS = {
    "web_search": "联网搜索",
    "fetch_url": "读取网页",
    "get_chime": "查看常数报时",
    "set_chime": "设置常数报时",
    "respond": "最终回复",
}

REGISTERED_TOOL_LABELS = {
    "create_reminder": "创建提醒",
    "list_reminders": "查看提醒",
    "cancel_reminder": "取消提醒",
    "search_sts2_knowledge": "查询 STS2 知识库",
    "get_group_context": "读取群上下文",
    "generate_daily_report": "生成日报/昨日总结",
    "get_group_status": "查看群状态",
    "get_group_profile": "查看群画像",
    "get_member_profile": "查看群友画像",
    "set_group_features": "调整群功能",
}

BUILTIN_CAPABILITIES = [
    AgentToolCapability(
        name="web_search",
        label=BUILTIN_TOOL_LABELS["web_search"],
        description="搜索当前、外部或实时信息。",
        category="web",
        source="builtin",
    ),
    AgentToolCapability(
        name="fetch_url",
        label=BUILTIN_TOOL_LABELS["fetch_url"],
        description="读取指定网页正文，通常配合联网搜索使用。",
        category="web",
        source="builtin",
    ),
    AgentToolCapability(
        name="get_chime",
        label=BUILTIN_TOOL_LABELS["get_chime"],
        description="查看当前会话的常数报时状态。",
        category="chime",
        source="builtin",
    ),
    AgentToolCapability(
        name="set_chime",
        label=BUILTIN_TOOL_LABELS["set_chime"],
        description="设置当前会话的常数报时；群聊中需要管理员权限。",
        category="chime",
        source="builtin",
        requires_admin=True,
    ),
    AgentToolCapability(
        name="respond",
        label=BUILTIN_TOOL_LABELS["respond"],
        description="结束工具调用并回复用户；这是 Agent 的基础能力，不能关闭。",
        category="core",
        source="builtin",
        configurable=False,
        default_enabled=True,
    ),
]

_db_ready = False


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def tool_definition_description(definition: dict[str, object]) -> str:
    function = definition.get("function")
    if not isinstance(function, dict):
        return ""
    return str(function.get("description") or "").strip()


def registered_tool_capabilities() -> list[AgentToolCapability]:
    capabilities: list[AgentToolCapability] = []
    for tool in list_agent_tools():
        capabilities.append(
            AgentToolCapability(
                name=tool.name,
                label=REGISTERED_TOOL_LABELS.get(tool.name, tool.name),
                description=tool_definition_description(tool.definition),
                category=tool.category,
                source="registered",
                requires_feature=tool.requires_feature,
                requires_admin=tool.requires_admin,
                requires_group=tool.requires_group,
            )
        )
    return capabilities


def all_agent_tool_capabilities() -> list[AgentToolCapability]:
    registered = {item.name: item for item in registered_tool_capabilities()}
    result = [copy.copy(item) for item in BUILTIN_CAPABILITIES]
    for name in sorted(registered):
        if name not in {item.name for item in result}:
            result.append(registered[name])
    return result


def get_agent_tool_capability(name: str) -> AgentToolCapability | None:
    for capability in all_agent_tool_capabilities():
        if capability.name == name:
            return capability
    tool = get_agent_tool(name)
    if tool is None:
        return None
    return AgentToolCapability(
        name=tool.name,
        label=REGISTERED_TOOL_LABELS.get(tool.name, tool.name),
        description=tool_definition_description(tool.definition),
        category=tool.category,
        source="registered",
        requires_feature=tool.requires_feature,
        requires_admin=tool.requires_admin,
        requires_group=tool.requires_group,
    )


def capability_to_dict(capability: AgentToolCapability) -> dict[str, object]:
    return {
        "name": capability.name,
        "label": capability.label,
        "description": capability.description,
        "category": capability.category,
        "source": capability.source,
        "requires_feature": capability.requires_feature or "",
        "requires_admin": capability.requires_admin,
        "requires_group": capability.requires_group,
        "configurable": capability.configurable,
        "default_enabled": capability.default_enabled,
    }


async def init_agent_tool_access_db() -> None:
    global _db_ready
    await init_access_db()
    async with aiosqlite.connect(ACCESS_DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS group_agent_tool_settings (
                group_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (group_id, tool_name)
            )
            """
        )
        await db.commit()
    _db_ready = True


async def ensure_agent_tool_access_db() -> None:
    if not _db_ready:
        await init_agent_tool_access_db()


async def group_agent_tool_rows(group_id: str | int) -> dict[str, bool]:
    await ensure_agent_tool_access_db()
    async with aiosqlite.connect(ACCESS_DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT tool_name, enabled
            FROM group_agent_tool_settings
            WHERE group_id = ?
            """,
            (str(group_id),),
        )
        rows = await cursor.fetchall()
    return {str(row[0]): bool(row[1]) for row in rows}


async def group_agent_tool_state(group_id: str | int) -> dict[str, object]:
    configured = await group_agent_tool_rows(group_id)
    tools: list[dict[str, object]] = []
    for capability in all_agent_tool_capabilities():
        enabled = configured.get(capability.name, capability.default_enabled)
        item = capability_to_dict(capability)
        item["enabled"] = bool(enabled)
        item["configured"] = capability.name in configured
        tools.append(item)
    return {"group_id": str(group_id), "tools": tools}


async def set_group_agent_tool(group_id: str | int, tool_name: str, enabled: bool) -> None:
    capability = get_agent_tool_capability(tool_name)
    if capability is None or not capability.configurable:
        return
    await ensure_agent_tool_access_db()
    async with aiosqlite.connect(ACCESS_DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO group_agent_tool_settings (group_id, tool_name, enabled, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(group_id, tool_name) DO UPDATE SET
                enabled = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (str(group_id), tool_name, 1 if enabled else 0, now_text()),
        )
        await db.commit()


async def save_group_agent_tool_state(group_id: str | int, payload: object) -> dict[str, object]:
    values = payload if isinstance(payload, dict) else {}
    for capability in all_agent_tool_capabilities():
        if not capability.configurable or capability.name not in values:
            continue
        await set_group_agent_tool(group_id, capability.name, bool(values[capability.name]))
    return await group_agent_tool_state(group_id)


async def agent_tool_enabled_for_group(group_id: str | int, tool_name: str) -> bool:
    capability = get_agent_tool_capability(tool_name)
    if capability is None:
        return False
    if not capability.configurable:
        return capability.default_enabled
    rows = await group_agent_tool_rows(group_id)
    return rows.get(tool_name, capability.default_enabled)


async def is_agent_tool_allowed(tool_name: str, context: dict[str, object]) -> tuple[bool, str]:
    capability = get_agent_tool_capability(tool_name)
    if capability is None:
        return False, f"未知 Agent 工具：{tool_name}"

    target_type = str(context.get("_target_type") or "")
    target_id = str(context.get("_target_id") or "")
    if capability.requires_group and target_type != "group":
        return False, f"{capability.label}只能在群聊中使用。"

    if capability.requires_admin and not bool(context.get("_is_admin")):
        return False, f"{capability.label}需要管理员权限。"

    if target_type == "group" and target_id:
        if not await agent_tool_enabled_for_group(target_id, tool_name):
            return False, f"当前群未允许 Agent 工具：{capability.label}。"
        if capability.requires_feature and not await is_group_feature_enabled(target_id, capability.requires_feature):
            return False, f"当前群未开启功能：{capability.requires_feature}。"

    return True, ""


async def filter_allowed_agent_tool_definitions(
    definitions: list[dict[str, object]],
    context: dict[str, object],
) -> list[dict[str, object]]:
    allowed_definitions: list[dict[str, object]] = []
    for definition in definitions:
        function = definition.get("function")
        name = str(function.get("name") or "") if isinstance(function, dict) else ""
        allowed, _ = await is_agent_tool_allowed(name, context)
        if allowed:
            allowed_definitions.append(copy.deepcopy(definition))
    return allowed_definitions


def agent_tool_capabilities_state() -> list[dict[str, object]]:
    return [capability_to_dict(item) for item in all_agent_tool_capabilities()]
