from __future__ import annotations

import asyncio
import hashlib
import json

import aiosqlite

from plugins.agent_tools import audit


def reset_audit_state(tmp_path, monkeypatch, *, hmac_key: str | None = "test-audit-key") -> None:
    monkeypatch.setattr(audit, "DB_PATH", tmp_path / "agent_tool_audit.db")
    monkeypatch.setattr(audit, "_fingerprint_key", None)
    monkeypatch.setattr(audit, "_fingerprint_key_source", "")
    if hmac_key is None:
        monkeypatch.delenv(audit.AUDIT_HMAC_KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(audit.AUDIT_HMAC_KEY_ENV, hmac_key)


async def rows() -> list[aiosqlite.Row]:
    async with aiosqlite.connect(audit.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM agent_tool_audit_events ORDER BY invocation_id, sequence"
        )
        return await cursor.fetchall()


def test_database_initialization_creates_append_only_event_table(tmp_path, monkeypatch) -> None:
    reset_audit_state(tmp_path, monkeypatch)

    async def run() -> list[str]:
        await audit.init_audit_db()
        async with aiosqlite.connect(audit.DB_PATH) as db:
            cursor = await db.execute("PRAGMA table_info(agent_tool_audit_events)")
            return [str(row[1]) for row in await cursor.fetchall()]

    columns = asyncio.run(run())

    assert audit.DB_PATH.is_file()
    assert columns == [
        "event_id",
        "invocation_id",
        "sequence",
        "occurred_at",
        "event_type",
        "tool_call_id",
        "tool_name",
        "invocation_source",
        "actor_user_id",
        "session_target_type",
        "session_target_id",
        "effective_group_id",
        "arguments_fingerprint",
        "confirmation_id",
        "confirmation_status",
        "idempotency_key",
        "idempotency_status",
        "risk_level",
        "side_effect",
        "execution_stage",
        "outcome",
        "error_code",
        "retryable",
        "failure_class",
        "duration_ms",
        "safe_details_json",
    ]


def test_append_event_writes_all_fields(tmp_path, monkeypatch) -> None:
    reset_audit_state(tmp_path, monkeypatch)
    invocation_id = audit.create_invocation_id()
    fingerprint = audit.fingerprint_arguments({"enabled": True, "mode": "hourly"})

    event = asyncio.run(
        audit.append_event(
            invocation_id=invocation_id,
            sequence=3,
            event_type="execution_completed",
            tool_call_id="call-1",
            tool_name="set_chime",
            invocation_source="agent",
            actor_user_id="10001",
            session_target_type="group",
            session_target_id="20001",
            effective_group_id="20001",
            arguments_fingerprint=fingerprint,
            confirmation_id=42,
            confirmation_status="consumed",
            idempotency_key="idem-key",
            idempotency_status="succeeded",
            risk_level="high",
            side_effect="external",
            execution_stage="completed",
            outcome="success",
            error_code="",
            retryable=False,
            failure_class="",
            duration_ms=25,
            safe_details={"enabled": True, "mode": "hourly"},
        )
    )
    stored = asyncio.run(rows())[0]

    assert event.invocation_id == invocation_id
    assert event.sequence == 3
    assert stored["event_id"] == event.event_id
    assert stored["tool_call_id"] == "call-1"
    assert stored["confirmation_id"] == 42
    assert stored["idempotency_key"] == "idem-key"
    assert stored["duration_ms"] == 25
    assert json.loads(stored["safe_details_json"]) == {"enabled": True, "mode": "hourly"}


def test_sensitive_details_are_redacted_and_url_query_is_removed(tmp_path, monkeypatch) -> None:
    reset_audit_state(tmp_path, monkeypatch)
    secrets_by_value = [
        "system prompt secret",
        "drink water at nine",
        "confirmation-token-value",
        "ABCD2345",
        "owner-token-value",
        "Alice Nickname",
        "search private words",
    ]
    details = {
        "prompt": secrets_by_value[0],
        "reminder_text": secrets_by_value[1],
        "confirmation_token": secrets_by_value[2],
        "confirmation_code": secrets_by_value[3],
        "idempotency_owner_token": secrets_by_value[4],
        "nickname": secrets_by_value[5],
        "query": secrets_by_value[6],
        "arguments": {"text": "nested reminder body"},
        "url": "https://user:password@example.com/path?token=top-secret#fragment",
        "enabled": True,
    }

    asyncio.run(
        audit.append_event(
            invocation_id=audit.create_invocation_id(),
            event_type="tool_requested",
            tool_name="fetch_url",
            safe_details=details,
        )
    )
    raw_json = str(asyncio.run(rows())[0]["safe_details_json"])
    stored = json.loads(raw_json)

    for value in secrets_by_value:
        assert value not in raw_json
    assert "nested reminder body" not in raw_json
    assert "top-secret" not in raw_json
    assert "password" not in raw_json
    assert stored["url"] == "https://example.com/path"
    assert stored["enabled"] is True


def test_fingerprint_is_stable_hmac_and_does_not_contain_plaintext(tmp_path, monkeypatch) -> None:
    reset_audit_state(tmp_path, monkeypatch, hmac_key="stable-key")
    arguments = {"text": "private reminder", "enabled": True}

    first = audit.fingerprint_arguments(arguments)
    second = audit.fingerprint_arguments({"enabled": True, "text": "private reminder"})
    bare_sha256 = hashlib.sha256(
        json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    assert first == second
    assert first != bare_sha256
    assert "private reminder" not in first
    assert len(first) == 64


def test_runtime_random_fingerprint_key_warns_and_is_not_restart_stable(tmp_path, monkeypatch) -> None:
    reset_audit_state(tmp_path, monkeypatch, hmac_key=None)
    warnings: list[str] = []
    monkeypatch.setattr(audit.logger, "warning", lambda message: warnings.append(str(message)))

    first = audit.fingerprint_arguments({"value": "same"})
    same_runtime = audit.fingerprint_arguments({"value": "same"})
    monkeypatch.setattr(audit, "_fingerprint_key", None)
    monkeypatch.setattr(audit, "_fingerprint_key_source", "")
    simulated_restart = audit.fingerprint_arguments({"value": "same"})

    assert first == same_runtime
    assert first != simulated_restart
    assert len(warnings) == 2
    assert all("cannot be correlated across restarts" in item for item in warnings)


def test_concurrent_appends_allocate_unique_sequences(tmp_path, monkeypatch) -> None:
    reset_audit_state(tmp_path, monkeypatch)
    invocation_id = audit.create_invocation_id()

    async def run() -> list[int]:
        events = await asyncio.gather(
            *[
                audit.append_event(
                    invocation_id=invocation_id,
                    event_type="execution_stage",
                    tool_name="build_semantic_graph",
                    execution_stage=f"stage-{index}",
                    safe_details={"count": index},
                )
                for index in range(20)
            ]
        )
        return sorted(event.sequence for event in events)

    sequences = asyncio.run(run())
    stored_rows = asyncio.run(rows())

    assert sequences == list(range(1, 21))
    assert [int(row["sequence"]) for row in stored_rows] == list(range(1, 21))
    assert len({str(row["event_id"]) for row in stored_rows}) == 20
