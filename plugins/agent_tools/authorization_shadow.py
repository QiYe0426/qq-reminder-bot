from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from nonebot.log import logger

from .authorization_policy import AuthorizationPolicyConflict, ResolvedAuthorizationPolicy


AuthorizationDecisionCategory = Literal[
    "ALLOW_ALLOW",
    "DENY_DENY",
    "LEGACY_DENY_METADATA_ALLOW",
    "LEGACY_ALLOW_METADATA_DENY",
]

AUTHORIZATION_V2_ENFORCEMENT_ENV = "AGENT_AUTHORIZATION_V2_ENFORCEMENT"


def authorization_v2_enforcement_enabled() -> bool:
    value = os.getenv(AUTHORIZATION_V2_ENFORCEMENT_ENV, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ShadowAuthorizationResult:
    allowed: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class AuthorizationDecisionComparison:
    category: AuthorizationDecisionCategory
    legacy_allowed: bool
    metadata_allowed: bool
    conflict_fields: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class MetadataAuthorizationDecision:
    allowed: bool
    reason_codes: tuple[str, ...]
    effective_group_id: str
    failed_requirements: tuple[str, ...]
    conflicts: tuple[AuthorizationPolicyConflict, ...]

    @property
    def reasons(self) -> tuple[str, ...]:
        return self.reason_codes


@dataclass(frozen=True)
class OwnershipPolicyRequest:
    tool_name: str
    policy_name: str
    arguments: dict[str, object]
    context: dict[str, object]
    effective_group_id: str


@dataclass(frozen=True)
class OwnershipPolicyResult:
    allowed: bool
    reason_code: str = ""


OwnershipPolicyAdapter = Callable[[OwnershipPolicyRequest], Awaitable[OwnershipPolicyResult]]


class CurrentUserOwnershipAdapter:
    """Verify current-user ownership using only trusted server context."""

    async def __call__(self, request: OwnershipPolicyRequest) -> OwnershipPolicyResult:
        current_user_id = str(request.context.get("_user_id") or "").strip()
        if not current_user_id:
            return OwnershipPolicyResult(False, "current_user_missing")

        if request.tool_name == "create_reminder":
            scope = request.context.get("_scope")
            owner_user_id = str(getattr(scope, "user_id", "") or "").strip()
        elif request.tool_name in {"list_reminders", "cancel_reminder"}:
            # These handlers scope their resource query with trusted _user_id;
            # no model-supplied owner argument participates in the lookup.
            owner_user_id = current_user_id
        else:
            return OwnershipPolicyResult(False, "current_user_owner_unresolved")

        if not owner_user_id:
            return OwnershipPolicyResult(False, "current_user_owner_unresolved")
        if owner_user_id != current_user_id:
            return OwnershipPolicyResult(False, "current_user_owner_mismatch")
        return OwnershipPolicyResult(True)


OWNERSHIP_POLICY_ADAPTERS: Mapping[str, OwnershipPolicyAdapter] = MappingProxyType(
    {"current_user": CurrentUserOwnershipAdapter()}
)


@dataclass(frozen=True)
class PermissionPolicyRequest:
    tool_name: str
    permission: str
    arguments: dict[str, object]
    context: dict[str, object]
    effective_group_id: str


@dataclass(frozen=True)
class PermissionPolicyResult:
    allowed: bool
    reason_code: str = ""


PermissionPolicyAdapter = Callable[[PermissionPolicyRequest], Awaitable[PermissionPolicyResult]]
PERMISSION_POLICY_ADAPTERS: Mapping[str, PermissionPolicyAdapter] = MappingProxyType({})


def _metadata_decision(
    policy: ResolvedAuthorizationPolicy,
    *,
    allowed: bool,
    reason_codes: tuple[str, ...] = (),
    effective_group_id: str = "",
    failed_requirements: tuple[str, ...] = (),
) -> MetadataAuthorizationDecision:
    return MetadataAuthorizationDecision(
        allowed=allowed,
        reason_codes=reason_codes,
        effective_group_id=effective_group_id,
        failed_requirements=failed_requirements,
        conflicts=policy.conflicts,
    )


async def evaluate_metadata_authorization(
    *,
    tool_name: str,
    arguments: dict[str, object],
    context: dict[str, object],
    policy: ResolvedAuthorizationPolicy,
    ownership_adapters: Mapping[str, OwnershipPolicyAdapter] | None = None,
    permission_adapters: Mapping[str, PermissionPolicyAdapter] | None = None,
) -> MetadataAuthorizationDecision:
    """Independently evaluate resolved Metadata Authorization requirements.

    Existing service primitives remain the single source for effective-group
    routing, target-admin verification, feature state, and per-group tool state.
    No legacy allow/deny result is accepted as input.
    """

    from plugins import agent_tool_access as access

    security_conflicts = tuple(
        conflict.field for conflict in policy.conflicts if conflict.severity == "security"
    )
    if security_conflicts:
        return _metadata_decision(
            policy,
            allowed=False,
            reason_codes=("security_policy_conflict",),
            failed_requirements=security_conflicts,
        )

    target_type = str(context.get("_target_type") or "")
    if target_type not in {"group", "private"}:
        return _metadata_decision(
            policy,
            allowed=False,
            reason_codes=("unsupported_session_type",),
            failed_requirements=("session_requirement",),
        )
    if policy.session_requirement == "deny_all":
        return _metadata_decision(
            policy,
            allowed=False,
            reason_codes=("session_requirement_denied",),
            failed_requirements=("session_requirement",),
        )
    if policy.session_requirement == "group_only" and target_type != "group":
        return _metadata_decision(
            policy,
            allowed=False,
            reason_codes=("group_session_required",),
            failed_requirements=("session_requirement",),
        )
    if policy.session_requirement == "private_only" and target_type != "private":
        return _metadata_decision(
            policy,
            allowed=False,
            reason_codes=("private_session_required",),
            failed_requirements=("session_requirement",),
        )

    if policy.group_requirement == "deny_all":
        return _metadata_decision(
            policy,
            allowed=False,
            reason_codes=("group_requirement_denied",),
            failed_requirements=("group_requirement",),
        )

    metadata_capability = access.AgentToolCapability(
        name=tool_name,
        label=tool_name,
        description="",
        category="metadata",
        source="metadata",
        requires_feature=None,
        requires_admin=False,
        requires_group=policy.session_requirement == "group_only",
        group_scope=policy.group_requirement,
        requires_target_group_admin=False,
        configurable=True,
        default_enabled=True,
    )
    effective_group_id = ""
    if policy.group_requirement in {"current", "private_explicit"}:
        scope = access.resolve_effective_group_id(metadata_capability, arguments, context)
        if not scope.allowed:
            return _metadata_decision(
                policy,
                allowed=False,
                reason_codes=(scope.error or "group_scope_denied",),
                failed_requirements=("group_requirement",),
            )
        effective_group_id = scope.effective_group_id
    elif target_type == "group":
        effective_group_id = str(context.get("_target_id") or "").strip()

    if "session_admin" in policy.admin_requirements and not bool(context.get("_is_admin")):
        return _metadata_decision(
            policy,
            allowed=False,
            reason_codes=("session_admin_required",),
            effective_group_id=effective_group_id,
            failed_requirements=("session_admin",),
        )

    if "target_group_admin" in policy.admin_requirements:
        if not effective_group_id:
            return _metadata_decision(
                policy,
                allowed=False,
                reason_codes=("missing_effective_group",),
                failed_requirements=("target_group_admin",),
            )
        try:
            target_admin = await access.target_group_admin_authorized(
                str(context.get("_user_id") or "").strip(),
                effective_group_id,
                context,
            )
        except Exception:
            target_admin = False
        if not target_admin:
            return _metadata_decision(
                policy,
                allowed=False,
                reason_codes=("target_group_admin_denied",),
                effective_group_id=effective_group_id,
                failed_requirements=("target_group_admin",),
            )

    unsupported_admins = policy.admin_requirements - {"session_admin", "target_group_admin"}
    if unsupported_admins:
        return _metadata_decision(
            policy,
            allowed=False,
            reason_codes=("unsupported_admin_requirement",),
            effective_group_id=effective_group_id,
            failed_requirements=tuple(sorted(unsupported_admins)),
        )

    permissions = permission_adapters if permission_adapters is not None else PERMISSION_POLICY_ADAPTERS
    for permission in sorted(policy.required_permissions):
        adapter = permissions.get(permission)
        if adapter is None:
            return _metadata_decision(
                policy,
                allowed=False,
                reason_codes=("permission_unresolved",),
                effective_group_id=effective_group_id,
                failed_requirements=(f"permission:{permission}",),
            )
        request = PermissionPolicyRequest(
            tool_name=tool_name,
            permission=permission,
            arguments=arguments,
            context=context,
            effective_group_id=effective_group_id,
        )
        try:
            permission_result = await adapter(request)
        except Exception:
            permission_result = PermissionPolicyResult(False, "permission_adapter_failed")
        if not permission_result.allowed:
            return _metadata_decision(
                policy,
                allowed=False,
                reason_codes=(permission_result.reason_code or "permission_denied",),
                effective_group_id=effective_group_id,
                failed_requirements=(f"permission:{permission}",),
            )

    if (
        policy.feature_requirements
        and not effective_group_id
        and policy.tool_switch_policy != "session_group_if_present"
    ):
        return _metadata_decision(
            policy,
            allowed=False,
            reason_codes=("missing_effective_group",),
            failed_requirements=tuple(
                f"feature:{feature}" for feature in sorted(policy.feature_requirements)
            ),
        )
    if policy.feature_requirements and effective_group_id:
        for feature in sorted(policy.feature_requirements):
            try:
                enabled = await access.is_group_feature_enabled(effective_group_id, feature)
            except Exception:
                enabled = False
            if not enabled:
                return _metadata_decision(
                    policy,
                    allowed=False,
                    reason_codes=("feature_disabled",),
                    effective_group_id=effective_group_id,
                    failed_requirements=(f"feature:{feature}",),
                )

    if policy.tool_switch_policy == "deny_all":
        return _metadata_decision(
            policy,
            allowed=False,
            reason_codes=("tool_switch_policy_denied",),
            effective_group_id=effective_group_id,
            failed_requirements=("tool_switch_policy",),
        )
    if policy.tool_switch_policy == "effective_group_required" and not effective_group_id:
        return _metadata_decision(
            policy,
            allowed=False,
            reason_codes=("missing_effective_group",),
            failed_requirements=("tool_switch_policy",),
        )
    should_check_tool = (
        policy.tool_switch_policy == "effective_group_required"
        or (policy.tool_switch_policy == "session_group_if_present" and target_type == "group")
    )
    if should_check_tool:
        try:
            tool_enabled = await access.agent_tool_enabled_for_group(effective_group_id, tool_name)
        except Exception:
            tool_enabled = False
        if not tool_enabled:
            return _metadata_decision(
                policy,
                allowed=False,
                reason_codes=("tool_not_allowed",),
                effective_group_id=effective_group_id,
                failed_requirements=("tool_switch_policy",),
            )

    adapters = ownership_adapters if ownership_adapters is not None else OWNERSHIP_POLICY_ADAPTERS
    for ownership_policy in sorted(policy.ownership_policies):
        adapter = adapters.get(ownership_policy)
        if adapter is None:
            return _metadata_decision(
                policy,
                allowed=False,
                reason_codes=("ownership_policy_unresolved",),
                effective_group_id=effective_group_id,
                failed_requirements=(f"ownership:{ownership_policy}",),
            )
        request = OwnershipPolicyRequest(
            tool_name=tool_name,
            policy_name=ownership_policy,
            arguments=arguments,
            context=context,
            effective_group_id=effective_group_id,
        )
        try:
            ownership = await adapter(request)
        except Exception:
            ownership = OwnershipPolicyResult(False, "ownership_adapter_failed")
        if not ownership.allowed:
            return _metadata_decision(
                policy,
                allowed=False,
                reason_codes=(ownership.reason_code or "ownership_denied",),
                effective_group_id=effective_group_id,
                failed_requirements=(f"ownership:{ownership_policy}",),
            )

    return _metadata_decision(
        policy,
        allowed=True,
        effective_group_id=effective_group_id,
    )


def evaluate_authorization_shadow(
    policy: ResolvedAuthorizationPolicy,
    *,
    legacy_allowed: bool,
    context: dict[str, object],
    effective_group_id: str,
) -> ShadowAuthorizationResult:
    """Evaluate only conservative Metadata additions above the legacy result.

    The real legacy decision is an explicit safety floor. This adapter does not
    reproduce database, feature-switch, target-admin, or group-routing logic
    owned by authorize_agent_tool(). Requirements already present in the legacy
    projection are trusted as having been evaluated by that function.
    """

    if not legacy_allowed:
        return ShadowAuthorizationResult(False, ("legacy_denied_floor",))

    security_conflicts = tuple(
        conflict.field for conflict in policy.conflicts if conflict.severity == "security"
    )
    if security_conflicts:
        return ShadowAuthorizationResult(False, ("security_policy_conflict",))

    legacy = policy.legacy_projection
    reasons: list[str] = []

    added_permissions = policy.required_permissions - legacy.required_permissions
    if added_permissions:
        reasons.append("unverified_required_permissions")

    added_features = policy.feature_requirements - legacy.feature_requirements
    if added_features:
        reasons.append("unverified_feature_requirements")

    added_admins = policy.admin_requirements - legacy.admin_requirements
    if "session_admin" in added_admins and not bool(context.get("_is_admin")):
        reasons.append("session_admin_required")
    if "target_group_admin" in added_admins:
        reasons.append("unverified_target_group_admin")
    if added_admins - {"session_admin", "target_group_admin"}:
        reasons.append("unverified_admin_requirements")

    if policy.session_requirement != legacy.session_requirement:
        target_type = str(context.get("_target_type") or "")
        if policy.session_requirement == "deny_all":
            reasons.append("session_requirement_denied")
        elif policy.session_requirement == "group_only" and target_type != "group":
            reasons.append("group_session_required")
        elif policy.session_requirement == "private_only" and target_type != "private":
            reasons.append("private_session_required")

    if policy.group_requirement != legacy.group_requirement:
        target_type = str(context.get("_target_type") or "")
        target_id = str(context.get("_target_id") or "")
        if policy.group_requirement == "deny_all":
            reasons.append("group_requirement_denied")
        elif policy.group_requirement == "current":
            if target_type != "group" or not effective_group_id or effective_group_id != target_id:
                reasons.append("current_group_binding_required")
        else:
            reasons.append("unverified_group_requirement")

    if policy.tool_switch_policy != legacy.tool_switch_policy:
        reasons.append("unverified_tool_switch_policy")

    added_ownership = policy.ownership_policies - legacy.ownership_policies
    if added_ownership:
        reasons.append("unverified_ownership_policy")

    return ShadowAuthorizationResult(not reasons, tuple(sorted(set(reasons))))


def compare_authorization_decisions(
    *,
    legacy_allowed: bool,
    metadata_result: ShadowAuthorizationResult,
    policy: ResolvedAuthorizationPolicy,
) -> AuthorizationDecisionComparison:
    if legacy_allowed and metadata_result.allowed:
        category: AuthorizationDecisionCategory = "ALLOW_ALLOW"
    elif not legacy_allowed and not metadata_result.allowed:
        category = "DENY_DENY"
    elif not legacy_allowed and metadata_result.allowed:
        category = "LEGACY_DENY_METADATA_ALLOW"
    else:
        category = "LEGACY_ALLOW_METADATA_DENY"
    return AuthorizationDecisionComparison(
        category=category,
        legacy_allowed=legacy_allowed,
        metadata_allowed=metadata_result.allowed,
        conflict_fields=tuple(sorted({conflict.field for conflict in policy.conflicts})),
        reason_codes=metadata_result.reasons,
    )


def log_authorization_shadow_comparison(
    *,
    invocation_id: str,
    tool_name: str,
    comparison: AuthorizationDecisionComparison,
) -> None:
    conflict_summary = ",".join(comparison.conflict_fields) or "none"
    reason_summary = ",".join(comparison.reason_codes) or "none"
    message = (
        "Agent authorization shadow: invocation_id=%s tool_name=%s "
        "category=%s conflicts=%s reasons=%s"
    )
    values = (
        invocation_id,
        tool_name,
        comparison.category,
        conflict_summary,
        reason_summary,
    )
    if comparison.category == "LEGACY_DENY_METADATA_ALLOW":
        logger.warning(message, *values)
    elif comparison.category == "LEGACY_ALLOW_METADATA_DENY":
        logger.info(message, *values)
    else:
        logger.debug(message, *values)


def observe_authorization_shadow(
    *,
    invocation_id: str,
    tool_name: str,
    policy: ResolvedAuthorizationPolicy,
    legacy_allowed: bool,
    context: dict[str, object],
    effective_group_id: str,
) -> AuthorizationDecisionComparison | None:
    """Best-effort observation that can never change or break legacy Runtime."""

    try:
        metadata_result = evaluate_authorization_shadow(
            policy,
            legacy_allowed=legacy_allowed,
            context=context,
            effective_group_id=effective_group_id,
        )
        comparison = compare_authorization_decisions(
            legacy_allowed=legacy_allowed,
            metadata_result=metadata_result,
            policy=policy,
        )
        log_authorization_shadow_comparison(
            invocation_id=invocation_id,
            tool_name=tool_name,
            comparison=comparison,
        )
        return comparison
    except Exception:
        logger.exception(
            "Agent authorization shadow observation failed: invocation_id=%s tool_name=%s",
            invocation_id,
            tool_name,
        )
        return None
