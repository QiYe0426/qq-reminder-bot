from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .registry import AgentTool, list_agent_tools


PolicySeverity = Literal["info", "warning", "security"]
PolicySource = Literal["metadata", "legacy", "conservative_merge"]
AuthorizationSource = Literal["legacy"]
RuntimeRiskLevel = Literal["low", "medium", "high"]
RuntimeSideEffect = Literal["none", "write", "external"]


@dataclass(frozen=True)
class PolicyConflict:
    field: str
    metadata_value: object
    legacy_value: object
    resolution: str
    reason: str
    severity: PolicySeverity


@dataclass(frozen=True)
class ResolvedAgentToolPolicy:
    # Runtime-compatible enforcement values.
    risk_level: RuntimeRiskLevel
    side_effect: RuntimeSideEffect
    requires_confirmation: bool
    confirmation_timeout: int
    idempotency_enabled: bool
    idempotency_ttl: int
    idempotency_lease_timeout: int
    idempotency_temporary_failure_ttl: int
    idempotency_temporary_errors: frozenset[str]
    idempotency_unknown_errors: frozenset[str]

    # Legacy authorization projection. Metadata resource_scope is declarative only.
    requires_feature: str | None
    requires_admin: bool
    requires_group: bool
    group_scope: str
    requires_target_group_admin: bool

    # Metadata v2 declarations.
    declared_risk_level: str
    declared_side_effect: str
    resource_scope: str
    confirmation_policy: str
    idempotency_policy: str
    timeout_seconds: int | None
    output_budget: int | None

    # Migration state.
    confirmation_source: PolicySource
    idempotency_source: PolicySource
    authorization_source: AuthorizationSource
    timeout_enforced: bool
    output_budget_enforced: bool

    conflicts: tuple[PolicyConflict, ...]


_RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_SIDE_EFFECT_ORDER = {"none": 0, "write": 1, "external": 2}
_SIDE_EFFECT_PROJECTION: dict[str, RuntimeSideEffect] = {
    "none": "none",
    "read": "none",
    "database_write": "write",
    "file_write": "write",
    "external_write": "external",
    "message_send": "external",
    "mixed": "external",
}


def _resolve_risk_level(tool: AgentTool) -> tuple[RuntimeRiskLevel, PolicyConflict | None]:
    declared = tool.metadata.risk_level
    legacy = tool.risk_level
    semantic_resolution = max((declared, legacy), key=_RISK_ORDER.__getitem__)
    runtime_resolution: RuntimeRiskLevel = "high" if semantic_resolution == "critical" else semantic_resolution  # type: ignore[assignment]

    if declared == legacy:
        return runtime_resolution, None
    if _RISK_ORDER[declared] < _RISK_ORDER[legacy]:
        reason = "Metadata risk is lower than the legacy Runtime risk; legacy protection is retained."
        severity: PolicySeverity = "security"
    elif declared == "critical":
        reason = "Critical is conservatively projected to high for compatibility with the current Gateway."
        severity = "warning"
    else:
        reason = "Metadata declares a higher risk; the more restrictive level is selected."
        severity = "info"
    return runtime_resolution, PolicyConflict(
        field="risk_level",
        metadata_value=declared,
        legacy_value=legacy,
        resolution=runtime_resolution,
        reason=reason,
        severity=severity,
    )


def _resolve_side_effect(tool: AgentTool) -> tuple[RuntimeSideEffect, PolicyConflict | None]:
    declared = tool.metadata.side_effect
    projected = _SIDE_EFFECT_PROJECTION[declared]
    legacy = tool.side_effect
    resolution: RuntimeSideEffect = max((projected, legacy), key=_SIDE_EFFECT_ORDER.__getitem__)  # type: ignore[assignment]
    if projected == legacy:
        return resolution, None

    metadata_is_weaker = _SIDE_EFFECT_ORDER[projected] < _SIDE_EFFECT_ORDER[legacy]
    return resolution, PolicyConflict(
        field="side_effect",
        metadata_value=declared,
        legacy_value=legacy,
        resolution=resolution,
        reason=(
            "Metadata side effect projects below the legacy value; legacy protection is retained."
            if metadata_is_weaker
            else "Metadata side effect projects above the legacy value; the more conservative value is selected."
        ),
        severity="security" if metadata_is_weaker else "info",
    )


def _resolve_confirmation(tool: AgentTool) -> tuple[bool, PolicySource, PolicyConflict | None]:
    policy = tool.metadata.confirmation_policy
    legacy = tool.requires_confirmation
    if policy == "required":
        if legacy:
            return True, "metadata", None
        return True, "metadata", PolicyConflict(
            "confirmation_policy",
            policy,
            legacy,
            "required",
            "Metadata adds confirmation; the stricter requirement is selected.",
            "info",
        )
    if policy == "never":
        if not legacy:
            return False, "metadata", None
        return True, "conservative_merge", PolicyConflict(
            "confirmation_policy",
            policy,
            legacy,
            "required",
            "Metadata cannot cancel a legacy confirmation requirement.",
            "security",
        )

    return legacy, "legacy", PolicyConflict(
        "confirmation_policy",
        policy,
        legacy,
        "legacy_required" if legacy else "legacy_not_required",
        f"{policy} has no Phase 2.0 Runtime semantics and falls back to legacy.",
        "warning",
    )


def _resolve_idempotency(tool: AgentTool) -> tuple[bool, PolicySource, PolicyConflict | None]:
    policy = tool.metadata.idempotency_policy
    legacy = tool.idempotency_enabled
    if policy == "result_cache":
        if legacy:
            return True, "metadata", None
        return True, "metadata", PolicyConflict(
            "idempotency_policy",
            policy,
            legacy,
            "enabled",
            "Metadata adds result-cache protection; idempotency is enabled.",
            "info",
        )
    if policy == "none":
        if not legacy:
            return False, "metadata", None
        return True, "conservative_merge", PolicyConflict(
            "idempotency_policy",
            policy,
            legacy,
            "enabled",
            "Metadata cannot disable legacy idempotency protection.",
            "security",
        )

    return legacy, "legacy", PolicyConflict(
        "idempotency_policy",
        policy,
        legacy,
        "legacy_enabled" if legacy else "legacy_disabled",
        "single_flight is not equivalent to the current cache-and-replay layer and falls back to legacy.",
        "warning",
    )


def resolve_agent_tool_policy(tool: AgentTool) -> ResolvedAgentToolPolicy:
    """Resolve Metadata v2 declarations against legacy policy without side effects."""

    risk_level, risk_conflict = _resolve_risk_level(tool)
    side_effect, side_effect_conflict = _resolve_side_effect(tool)
    requires_confirmation, confirmation_source, confirmation_conflict = _resolve_confirmation(tool)
    idempotency_enabled, idempotency_source, idempotency_conflict = _resolve_idempotency(tool)
    conflicts = tuple(
        conflict
        for conflict in (risk_conflict, side_effect_conflict, confirmation_conflict, idempotency_conflict)
        if conflict is not None
    )
    metadata = tool.metadata

    return ResolvedAgentToolPolicy(
        risk_level=risk_level,
        side_effect=side_effect,
        requires_confirmation=requires_confirmation,
        confirmation_timeout=tool.confirmation_timeout,
        idempotency_enabled=idempotency_enabled,
        idempotency_ttl=tool.idempotency_ttl,
        idempotency_lease_timeout=tool.idempotency_lease_timeout,
        idempotency_temporary_failure_ttl=tool.idempotency_temporary_failure_ttl,
        idempotency_temporary_errors=tool.idempotency_temporary_errors,
        idempotency_unknown_errors=tool.idempotency_unknown_errors,
        requires_feature=tool.requires_feature,
        requires_admin=tool.requires_admin,
        requires_group=tool.requires_group,
        group_scope=tool.group_scope,
        requires_target_group_admin=tool.requires_target_group_admin,
        declared_risk_level=metadata.risk_level,
        declared_side_effect=metadata.side_effect,
        resource_scope=metadata.resource_scope,
        confirmation_policy=metadata.confirmation_policy,
        idempotency_policy=metadata.idempotency_policy,
        timeout_seconds=metadata.timeout_seconds,
        output_budget=metadata.output_budget,
        confirmation_source=confirmation_source,
        idempotency_source=idempotency_source,
        authorization_source="legacy",
        timeout_enforced=metadata.timeout_seconds is not None,
        output_budget_enforced=False,
        conflicts=conflicts,
    )


def resolve_all_registered_tool_policies() -> tuple[tuple[str, ResolvedAgentToolPolicy], ...]:
    """Return a deterministic immutable snapshot for tests and shadow comparisons."""

    return tuple(
        (tool.name, resolve_agent_tool_policy(tool))
        for tool in sorted(list_agent_tools(), key=lambda item: item.name)
    )
