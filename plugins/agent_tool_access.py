from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime

import aiosqlite
from nonebot.log import logger

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
    group_scope: str = "none"
    requires_target_group_admin: bool = False
    configurable: bool = True
    default_enabled: bool = True


@dataclass(frozen=True)
class AgentToolAuthorization:
    allowed: bool
    error: str = ""
    message: str = ""
    effective_group_id: str = ""


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
    "build_semantic_graph": "生成语义图",
    "get_semantic_graph": "读取语义图",
    "render_semantic_graph": "语义图可视化",
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
                group_scope=tool.group_scope,
                requires_target_group_admin=tool.requires_target_group_admin,
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
        group_scope=tool.group_scope,
        requires_target_group_admin=tool.requires_target_group_admin,
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
        "group_scope": capability.group_scope,
        "requires_target_group_admin": capability.requires_target_group_admin,
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


async def target_group_admin_authorized(
    user_id: str,
    group_id: str,
    context: dict[str, object],
) -> bool:
    if not user_id.isdigit() or not group_id.isdigit():
        return False

    if (
        str(context.get("_target_type") or "") == "group"
        and str(context.get("_target_id") or "") == group_id
    ):
        event = context.get("_event")
        sender = getattr(event, "sender", None)
        role = str(getattr(sender, "role", "") or "").strip().lower()
        if role:
            return role in {"owner", "admin"}

    try:
        from nonebot import get_bot

        bot = get_bot()
        member = await bot.call_api(
            "get_group_member_info",
            group_id=int(group_id),
            user_id=int(user_id),
            no_cache=True,
        )
    except Exception:
        from plugins.sensitive_logging import log_fingerprint

        logger.warning(
            "Unable to verify target group admin role: scope_fingerprint={} actor_fingerprint={}",
            log_fingerprint("group_id", group_id),
            log_fingerprint("user_id", user_id),
        )
        return False

    if not isinstance(member, dict):
        return False
    return str(member.get("role") or "").strip().lower() in {"owner", "admin"}


def resolve_effective_group_id(
    capability: AgentToolCapability,
    arguments: dict[str, object],
    context: dict[str, object],
) -> AgentToolAuthorization:
    target_type = str(context.get("_target_type") or "")
    current_group_id = str(context.get("_target_id") or "").strip() if target_type == "group" else ""
    argument_group_id = str(arguments.get("group_id") or "").strip()

    if capability.group_scope == "current":
        if not current_group_id:
            return AgentToolAuthorization(False, "missing_group_id", "这个工具只能使用当前群资源。")
        if argument_group_id and argument_group_id != current_group_id:
            return AgentToolAuthorization(False, "group_permission_denied", "不能从当前群切换到其他群。")
        return AgentToolAuthorization(True, effective_group_id=current_group_id)

    if capability.group_scope == "private_explicit":
        if target_type == "group":
            if not current_group_id:
                return AgentToolAuthorization(False, "missing_group_id", "缺少当前群上下文。")
            if argument_group_id and argument_group_id != current_group_id:
                return AgentToolAuthorization(False, "group_permission_denied", "不能从当前群切换到其他群。")
            return AgentToolAuthorization(True, effective_group_id=current_group_id)
        if target_type == "private":
            if not argument_group_id:
                return AgentToolAuthorization(False, "missing_group_id", "私聊调用群工具时必须提供 group_id。")
            if not argument_group_id.isdigit():
                return AgentToolAuthorization(False, "invalid_group_id", "group_id 必须是有效的 QQ 群号。")
            return AgentToolAuthorization(True, effective_group_id=argument_group_id)
        return AgentToolAuthorization(False, "missing_group_id", "缺少可用的群会话上下文。")

    return AgentToolAuthorization(True)


async def authorize_agent_tool(
    tool_name: str,
    arguments: dict[str, object],
    context: dict[str, object],
) -> AgentToolAuthorization:
    capability = get_agent_tool_capability(tool_name)
    if capability is None:
        return AgentToolAuthorization(False, "tool_not_found", f"未知 Agent 工具：{tool_name}")

    if capability.group_scope == "none":
        allowed, reason = await is_agent_tool_allowed(tool_name, context)
        return AgentToolAuthorization(allowed, "" if allowed else "tool_not_allowed", reason)

    target_type = str(context.get("_target_type") or "")
    if capability.requires_group and target_type != "group":
        return AgentToolAuthorization(False, "group_permission_denied", f"{capability.label}只能在群聊中使用。")
    if capability.requires_admin and not bool(context.get("_is_admin")):
        return AgentToolAuthorization(False, "group_permission_denied", f"{capability.label}需要管理员权限。")

    scope = resolve_effective_group_id(capability, arguments, context)
    if not scope.allowed:
        return scope
    group_id = scope.effective_group_id

    if capability.requires_target_group_admin:
        user_id = str(context.get("_user_id") or "").strip()
        if not await target_group_admin_authorized(user_id, group_id, context):
            return AgentToolAuthorization(
                False,
                "group_permission_denied",
                "你不是目标群的群主或管理员，无法操作该群资源。",
            )

    if not await agent_tool_enabled_for_group(group_id, tool_name):
        return AgentToolAuthorization(False, "tool_not_allowed", f"目标群未允许 Agent 工具：{capability.label}。")
    if capability.requires_feature and not await is_group_feature_enabled(group_id, capability.requires_feature):
        return AgentToolAuthorization(False, "feature_disabled", f"目标群未开启功能：{capability.requires_feature}。")

    return AgentToolAuthorization(True, effective_group_id=group_id)


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
