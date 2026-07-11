from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from .registry import AgentTool, list_agent_tools


@dataclass(frozen=True)
class _UnsetValue:
    def __repr__(self) -> str:
        return "UNSET"


UNSET = _UnsetValue()
UnsetValue = _UnsetValue

AuthorizationSource = Literal["legacy", "metadata", "conservative_merge"]
ConflictSeverity = Literal["info", "warning", "security"]
SessionRequirement = Literal["any", "group_only", "private_only", "deny_all"]
GroupRequirement = Literal["none", "current", "private_explicit", "deny_all"]
ToolSwitchPolicy = Literal[
    "not_required",
    "session_group_if_present",
    "effective_group_required",
    "deny_all",
]


@dataclass(frozen=True)
class AuthorizationMetadata:
    target_scope: str | UnsetValue = UNSET
    required_permissions: frozenset[str] | UnsetValue = UNSET
    feature_requirements: frozenset[str] | UnsetValue = UNSET
    admin_requirements: frozenset[str] | UnsetValue = UNSET
    session_requirement: SessionRequirement | UnsetValue = UNSET
    group_requirement: GroupRequirement | UnsetValue = UNSET
    tool_switch_policy: ToolSwitchPolicy | UnsetValue = UNSET
    ownership_policy: str | UnsetValue = UNSET


@dataclass(frozen=True)
class LegacyAuthorizationProjection:
    required_permissions: frozenset[str]
    feature_requirements: frozenset[str]
    admin_requirements: frozenset[str]
    session_requirement: SessionRequirement
    group_requirement: GroupRequirement
    tool_switch_policy: ToolSwitchPolicy
    ownership_policies: frozenset[str]


@dataclass(frozen=True)
class AuthorizationPolicyConflict:
    field: str
    metadata_value: object
    legacy_value: object
    resolution: str
    reason: str
    severity: ConflictSeverity


@dataclass(frozen=True)
class ResolvedAuthorizationPolicy:
    metadata_declaration: AuthorizationMetadata
    legacy_projection: LegacyAuthorizationProjection
    target_scope: str | UnsetValue
    required_permissions: frozenset[str]
    feature_requirements: frozenset[str]
    admin_requirements: frozenset[str]
    session_requirement: SessionRequirement
    group_requirement: GroupRequirement
    tool_switch_policy: ToolSwitchPolicy
    ownership_policies: frozenset[str]
    source: AuthorizationSource
    conflicts: tuple[AuthorizationPolicyConflict, ...]


def _declaration(
    *,
    target_scope: str,
    feature: str | None = None,
    session_admin: bool = False,
    target_group_admin: bool = False,
    session_requirement: SessionRequirement = "any",
    group_requirement: GroupRequirement = "none",
    tool_switch_policy: ToolSwitchPolicy = "session_group_if_present",
    ownership_policy: str | UnsetValue = UNSET,
) -> AuthorizationMetadata:
    admins = set()
    if session_admin:
        admins.add("session_admin")
    if target_group_admin:
        admins.add("target_group_admin")
    return AuthorizationMetadata(
        target_scope=target_scope,
        required_permissions=frozenset(),
        feature_requirements=frozenset({feature}) if feature else frozenset(),
        admin_requirements=frozenset(admins),
        session_requirement=session_requirement,
        group_requirement=group_requirement,
        tool_switch_policy=tool_switch_policy,
        ownership_policy=ownership_policy,
    )


# Phase 2.4-B declarations mirror current legacy authorization. This catalog is
# shadow-only: authorize_agent_tool() does not import or consume it.
AUTHORIZATION_METADATA = MappingProxyType(
    {
        "build_semantic_graph": _declaration(
            target_scope="target_group",
            feature="collector",
            session_admin=True,
            target_group_admin=True,
            group_requirement="private_explicit",
            tool_switch_policy="effective_group_required",
        ),
        "cancel_reminder": _declaration(
            target_scope="user", feature="ai_chat", ownership_policy="current_user"
        ),
        "create_reminder": _declaration(
            target_scope="user", feature="ai_chat", ownership_policy="current_user"
        ),
        "generate_daily_report": _declaration(
            target_scope="target_group",
            feature="daily_report",
            session_admin=True,
            target_group_admin=True,
            group_requirement="private_explicit",
            tool_switch_policy="effective_group_required",
        ),
        "get_group_context": _declaration(
            target_scope="current_group",
            feature="ai_chat",
            session_requirement="group_only",
        ),
        "get_group_profile": _declaration(
            target_scope="target_group",
            feature="companion",
            session_admin=True,
            target_group_admin=True,
            group_requirement="private_explicit",
            tool_switch_policy="effective_group_required",
        ),
        "get_group_status": _declaration(
            target_scope="target_group",
            feature="ai_chat",
            session_admin=True,
            target_group_admin=True,
            group_requirement="private_explicit",
            tool_switch_policy="effective_group_required",
        ),
        "get_member_profile": _declaration(
            target_scope="target_group",
            feature="companion",
            session_admin=True,
            target_group_admin=True,
            group_requirement="private_explicit",
            tool_switch_policy="effective_group_required",
        ),
        "get_semantic_graph": _declaration(
            target_scope="target_group",
            feature="collector",
            session_admin=True,
            target_group_admin=True,
            group_requirement="private_explicit",
            tool_switch_policy="effective_group_required",
        ),
        "list_reminders": _declaration(
            target_scope="user", feature="ai_chat", ownership_policy="current_user"
        ),
        "render_semantic_graph": _declaration(
            target_scope="target_group",
            feature="collector",
            session_admin=True,
            target_group_admin=True,
            group_requirement="private_explicit",
            tool_switch_policy="effective_group_required",
        ),
        "search_sts2_knowledge": _declaration(target_scope="none", feature="ai_chat"),
        "set_chime": _declaration(target_scope="current_group", session_admin=True),
        "set_group_features": _declaration(
            target_scope="target_group",
            session_admin=True,
            target_group_admin=True,
            group_requirement="private_explicit",
            tool_switch_policy="effective_group_required",
        ),
    }
)


def _legacy_projection(tool: AgentTool) -> LegacyAuthorizationProjection:
    admins = set()
    if tool.requires_admin:
        admins.add("session_admin")
    if tool.requires_target_group_admin:
        admins.add("target_group_admin")
    tool_switch: ToolSwitchPolicy = (
        "effective_group_required"
        if tool.group_scope in {"current", "private_explicit"}
        else "session_group_if_present"
    )
    return LegacyAuthorizationProjection(
        required_permissions=frozenset(),
        feature_requirements=frozenset({tool.requires_feature}) if tool.requires_feature else frozenset(),
        admin_requirements=frozenset(admins),
        session_requirement="group_only" if tool.requires_group else "any",
        group_requirement=tool.group_scope,
        tool_switch_policy=tool_switch,
        ownership_policies=frozenset(),
    )


def _missing_legacy_conflict(
    field: str,
    metadata_value: object,
    legacy_value: object,
    resolution: object,
) -> AuthorizationPolicyConflict:
    return AuthorizationPolicyConflict(
        field=field,
        metadata_value=metadata_value,
        legacy_value=legacy_value,
        resolution=str(resolution),
        reason="Metadata cannot remove a legacy authorization requirement.",
        severity="security",
    )


def _merge_set(
    field: str,
    legacy: frozenset[str],
    declared: frozenset[str] | UnsetValue,
) -> tuple[frozenset[str], AuthorizationPolicyConflict | None]:
    if declared is UNSET:
        return legacy, None
    resolved = legacy | declared
    if legacy.issubset(declared):
        return resolved, None
    return resolved, _missing_legacy_conflict(field, declared, legacy, resolved)


def _merge_session_requirement(
    legacy: SessionRequirement,
    declared: SessionRequirement | UnsetValue,
) -> tuple[SessionRequirement, AuthorizationPolicyConflict | None]:
    if declared is UNSET or declared == legacy:
        return legacy, None
    if declared == "deny_all":
        return "deny_all", None
    if legacy == "any":
        return declared, None
    if declared == "any":
        return legacy, _missing_legacy_conflict("session_requirement", declared, legacy, legacy)
    return "deny_all", AuthorizationPolicyConflict(
        field="session_requirement",
        metadata_value=declared,
        legacy_value=legacy,
        resolution="deny_all",
        reason="Group-only and private-only session requirements are incompatible.",
        severity="security",
    )


def _merge_group_requirement(
    legacy: GroupRequirement,
    declared: GroupRequirement | UnsetValue,
) -> tuple[GroupRequirement, AuthorizationPolicyConflict | None]:
    if declared is UNSET or declared == legacy:
        return legacy, None
    if declared == "deny_all":
        return "deny_all", None
    if legacy == "current" and declared in {"none", "private_explicit"}:
        return legacy, _missing_legacy_conflict("group_requirement", declared, legacy, legacy)
    if legacy == "private_explicit" and declared == "current":
        return "current", None
    if legacy == "private_explicit" and declared == "none":
        return legacy, _missing_legacy_conflict("group_requirement", declared, legacy, legacy)
    if legacy == "none" and declared == "current":
        return "current", None
    return "deny_all", AuthorizationPolicyConflict(
        field="group_requirement",
        metadata_value=declared,
        legacy_value=legacy,
        resolution="deny_all",
        reason="The legacy and Metadata group routing requirements are not provably compatible.",
        severity="security",
    )


def _merge_tool_switch(
    legacy: ToolSwitchPolicy,
    declared: ToolSwitchPolicy | UnsetValue,
) -> tuple[ToolSwitchPolicy, AuthorizationPolicyConflict | None]:
    if declared is UNSET or declared == legacy:
        return legacy, None
    if declared == "deny_all":
        return "deny_all", None
    return legacy, AuthorizationPolicyConflict(
        field="tool_switch_policy",
        metadata_value=declared,
        legacy_value=legacy,
        resolution=legacy,
        reason="Tool-switch scopes are not interchangeable; the legacy check is retained.",
        severity="security",
    )


def resolve_authorization_policy_with_metadata(
    tool: AgentTool,
    metadata: AuthorizationMetadata,
) -> ResolvedAuthorizationPolicy:
    """Pure compatibility resolver used by the public catalog-backed entrypoint."""

    legacy = _legacy_projection(tool)
    conflicts: list[AuthorizationPolicyConflict] = []
    permissions, conflict = _merge_set(
        "required_permissions", legacy.required_permissions, metadata.required_permissions
    )
    if conflict:
        conflicts.append(conflict)
    features, conflict = _merge_set(
        "feature_requirements", legacy.feature_requirements, metadata.feature_requirements
    )
    if conflict:
        conflicts.append(conflict)
    admins, conflict = _merge_set(
        "admin_requirements", legacy.admin_requirements, metadata.admin_requirements
    )
    if conflict:
        conflicts.append(conflict)
    session, conflict = _merge_session_requirement(legacy.session_requirement, metadata.session_requirement)
    if conflict:
        conflicts.append(conflict)
    group, conflict = _merge_group_requirement(legacy.group_requirement, metadata.group_requirement)
    if conflict:
        conflicts.append(conflict)
    tool_switch, conflict = _merge_tool_switch(legacy.tool_switch_policy, metadata.tool_switch_policy)
    if conflict:
        conflicts.append(conflict)

    ownership = legacy.ownership_policies
    if metadata.ownership_policy is not UNSET:
        ownership = ownership | frozenset({metadata.ownership_policy})

    authorization_fields = (
        metadata.required_permissions,
        metadata.feature_requirements,
        metadata.admin_requirements,
        metadata.session_requirement,
        metadata.group_requirement,
        metadata.tool_switch_policy,
        metadata.ownership_policy,
    )
    has_metadata_requirements = any(value is not UNSET for value in authorization_fields)
    source: AuthorizationSource = "conservative_merge" if has_metadata_requirements else "legacy"

    return ResolvedAuthorizationPolicy(
        metadata_declaration=metadata,
        legacy_projection=legacy,
        target_scope=metadata.target_scope,
        required_permissions=permissions,
        feature_requirements=features,
        admin_requirements=admins,
        session_requirement=session,
        group_requirement=group,
        tool_switch_policy=tool_switch,
        ownership_policies=ownership,
        source=source,
        conflicts=tuple(conflicts),
    )


def resolve_authorization_policy(tool: AgentTool) -> ResolvedAuthorizationPolicy:
    metadata = AUTHORIZATION_METADATA.get(
        tool.name,
        AuthorizationMetadata(target_scope=tool.metadata.resource_scope),
    )
    return resolve_authorization_policy_with_metadata(tool, metadata)


def resolve_all_registered_authorization_policies() -> tuple[tuple[str, ResolvedAuthorizationPolicy], ...]:
    return tuple(
        (tool.name, resolve_authorization_policy(tool))
        for tool in sorted(list_agent_tools(), key=lambda item: item.name)
    )
