from __future__ import annotations

import asyncio
from collections import Counter, defaultdict

import aiosqlite

from plugins import agent_tool_access
from plugins.agent_tool_access import AgentToolAuthorization
from plugins.agent_tools import audit, gateway
from plugins.agent_tools.policy import resolve_agent_tool_policy
from plugins.agent_tools.registry import AgentTool, AgentToolMetadata


def _definition(name: str) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Phase 2.1 Audit policy source test tool.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    }


def _tool(
    name: str,
    handler,
    *,
    legacy_risk: str,
    legacy_side_effect: str,
    metadata: AgentToolMetadata,
) -> AgentTool:
    # Keep execution policies neutral: this test isolates Audit policy sourcing.
    return AgentTool(
        name=name,
        definition=_definition(name),
        handler=handler,
        risk_level=legacy_risk,
        side_effect=legacy_side_effect,
        requires_confirmation=False,
        idempotency_enabled=False,
        metadata=metadata,
    )


def _context(tool_name: str) -> dict[str, object]:
    return {
        "_user_id": "policy-source-user",
        "_target_type": "group",
        "_target_id": "policy-source-group",
        "_tool_call_id": f"call:{tool_name}",
        "_invocation_source": "agent",
    }


async def _audit_rows() -> list[aiosqlite.Row]:
    async with aiosqlite.connect(audit.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM agent_tool_audit_events ORDER BY occurred_at, invocation_id, sequence"
        )
        return await cursor.fetchall()


def test_audit_uses_resolved_policy(monkeypatch) -> None:
    handler_calls: Counter[str] = Counter()
    authorization_calls: Counter[str] = Counter()
    resolver_calls: Counter[str] = Counter()
    confirmation_calls = 0
    idempotency_calls = 0

    def handler_for(name: str):
        async def handler(arguments, context):
            handler_calls[name] += 1
            return {"ok": True, "tool": name}

        return handler

    tools = {
        "generate_daily_report": _tool(
            "generate_daily_report",
            handler_for("generate_daily_report"),
            legacy_risk="high",
            legacy_side_effect="external",
            metadata=AgentToolMetadata(
                risk_level="medium",
                side_effect="mixed",
                confirmation_policy="never",
                idempotency_policy="none",
            ),
        ),
        "build_semantic_graph": _tool(
            "build_semantic_graph",
            handler_for("build_semantic_graph"),
            legacy_risk="high",
            legacy_side_effect="write",
            metadata=AgentToolMetadata(
                risk_level="medium",
                side_effect="database_write",
                confirmation_policy="never",
                idempotency_policy="none",
            ),
        ),
        "set_group_features": _tool(
            "set_group_features",
            handler_for("set_group_features"),
            legacy_risk="high",
            legacy_side_effect="write",
            metadata=AgentToolMetadata(
                risk_level="high",
                side_effect="external_write",
                confirmation_policy="never",
                idempotency_policy="none",
            ),
        ),
    }

    monkeypatch.setattr(gateway, "get_agent_tool", tools.get)
    original_resolver = resolve_agent_tool_policy

    def counted_resolver(tool):
        resolver_calls[tool.name] += 1
        return original_resolver(tool)

    monkeypatch.setattr(gateway, "resolve_agent_tool_policy", counted_resolver)

    async def authorize(tool_name, arguments, context):
        authorization_calls[tool_name] += 1
        return AgentToolAuthorization(True, effective_group_id="policy-source-group")

    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", authorize)

    async def unexpected_confirmation(*args, **kwargs):
        nonlocal confirmation_calls
        confirmation_calls += 1
        raise AssertionError("Neutral confirmation policy must not invoke the state machine")

    async def unexpected_idempotency(*args, **kwargs):
        nonlocal idempotency_calls
        idempotency_calls += 1
        raise AssertionError("Neutral idempotency policy must not invoke the state machine")

    monkeypatch.setattr(gateway, "create_pending_confirmation", unexpected_confirmation)
    monkeypatch.setattr(gateway, "claim_execution", unexpected_idempotency)

    async def run_all():
        return {
            name: await gateway.execute_tool(name, {}, _context(name))
            for name in tools
        }

    results = asyncio.run(run_all())
    rows = asyncio.run(_audit_rows())
    rows_by_tool: dict[str, list[aiosqlite.Row]] = defaultdict(list)
    for row in rows:
        rows_by_tool[str(row["tool_name"])].append(row)

    assert all(result["ok"] is True for result in results.values())
    assert handler_calls == Counter({name: 1 for name in tools})
    assert authorization_calls == Counter({name: 1 for name in tools})
    assert resolver_calls == Counter({name: 1 for name in tools})
    assert confirmation_calls == 0
    assert idempotency_calls == 0

    assert {row["risk_level"] for row in rows_by_tool["generate_daily_report"]} == {"high"}
    assert {row["side_effect"] for row in rows_by_tool["generate_daily_report"]} == {"external"}
    assert {row["risk_level"] for row in rows_by_tool["build_semantic_graph"]} == {"high"}
    assert {row["side_effect"] for row in rows_by_tool["build_semantic_graph"]} == {"write"}
    assert {row["risk_level"] for row in rows_by_tool["set_group_features"]} == {"high"}
    assert {row["side_effect"] for row in rows_by_tool["set_group_features"]} == {"external"}


def test_resolved_high_risk_still_fails_closed_when_audit_is_unavailable(monkeypatch) -> None:
    handler_calls = 0
    resolver_calls = 0

    async def handler(arguments, context):
        nonlocal handler_calls
        handler_calls += 1
        return {"ok": True}

    tool = _tool(
        "resolved_high_audit_failure",
        handler,
        legacy_risk="low",
        legacy_side_effect="none",
        metadata=AgentToolMetadata(risk_level="critical"),
    )
    monkeypatch.setattr(gateway, "get_agent_tool", lambda name: tool)
    original_resolver = resolve_agent_tool_policy

    def counted_resolver(value):
        nonlocal resolver_calls
        resolver_calls += 1
        return original_resolver(value)

    monkeypatch.setattr(gateway, "resolve_agent_tool_policy", counted_resolver)

    async def authorize(tool_name, arguments, context):
        return AgentToolAuthorization(True, effective_group_id="policy-source-group")

    monkeypatch.setattr(agent_tool_access, "authorize_agent_tool", authorize)
    original_append = audit.append_event

    async def fail_execution_started(**values):
        if values["event_type"] == "execution_started":
            raise RuntimeError("audit unavailable")
        return await original_append(**values)

    monkeypatch.setattr(gateway.audit, "append_event", fail_execution_started)

    result = asyncio.run(gateway.execute_tool(tool.name, {}, _context(tool.name)))
    rows = asyncio.run(_audit_rows())

    assert result["error"] == "audit_unavailable"
    assert result["retryable"] is True
    assert handler_calls == 0
    assert resolver_calls == 1
    assert {row["risk_level"] for row in rows} == {"high"}
    assert [row["event_type"] for row in rows] == ["tool_requested", "execution_failed"]
