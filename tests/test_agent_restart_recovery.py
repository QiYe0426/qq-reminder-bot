from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import aiosqlite

from plugins.agent_tools import audit, confirmation, idempotency


PERSISTENT_KEY = "13579bdf02468ace" * 4


def run_child(script: str, environment: dict[str, str]) -> dict[str, object]:
    child_environment = os.environ.copy()
    child_environment.update(environment)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=child_environment,
        check=True,
        capture_output=True,
        text=True,
    )
    result_line = next(line for line in completed.stdout.splitlines() if line.startswith("RESULT="))
    return json.loads(result_line.removeprefix("RESULT="))


def test_pending_confirmation_survives_process_restart(tmp_path, monkeypatch) -> None:
    database = tmp_path / "confirmations.db"
    monkeypatch.setattr(confirmation, "DB_PATH", database)
    pending = asyncio.run(
        confirmation.create_pending_confirmation(
            tool_name="restart_confirmation",
            arguments={"value": "same"},
            user_id="restart-user",
            target_type="private",
            target_id="restart-user",
            effective_group_id="",
            timeout_seconds=300,
        )
    )
    script = """
import asyncio, json, os
from pathlib import Path
import nonebot
nonebot.init()
from plugins.agent_tools import confirmation
confirmation.DB_PATH = Path(os.environ["TEST_DB_PATH"])
result = asyncio.run(confirmation.confirm_pending_confirmation(
    os.environ["TEST_CONFIRMATION_CODE"],
    user_id="restart-user",
    target_type="private",
    target_id="restart-user",
))
print("RESULT=" + json.dumps({"ok": result.ok, "status": result.confirmation.status if result.confirmation else ""}))
"""

    result = run_child(
        script,
        {
            "TEST_DB_PATH": str(database),
            "TEST_CONFIRMATION_CODE": pending.confirmation_code,
        },
    )

    assert result == {"ok": True, "status": "confirmed"}


def test_cached_idempotency_result_survives_process_restart(tmp_path, monkeypatch) -> None:
    database = tmp_path / "executions.db"
    monkeypatch.setattr(idempotency, "DB_PATH", database)
    binding = idempotency.create_execution_binding(
        tool_name="restart_idempotency",
        arguments={"value": "same"},
        user_id="restart-user",
        target_type="private",
        target_id="restart-user",
        effective_group_id="",
    )

    async def persist_result() -> None:
        claim = await idempotency.claim_execution(binding, ttl_seconds=300, lease_seconds=60)
        assert claim.action == "execute"
        completed = await idempotency.complete_execution(
            binding,
            owner_token=claim.owner_token,
            result={"ok": True, "value": "persisted"},
            failure_class=idempotency.FAILURE_PERMANENT,
            ttl_seconds=300,
            temporary_failure_ttl_seconds=15,
        )
        assert completed is True

    asyncio.run(persist_result())
    script = """
import asyncio, json, os
from pathlib import Path
import nonebot
nonebot.init()
from plugins.agent_tools import idempotency
idempotency.DB_PATH = Path(os.environ["TEST_DB_PATH"])
binding = idempotency.create_execution_binding(
    tool_name="restart_idempotency",
    arguments={"value": "same"},
    user_id="restart-user",
    target_type="private",
    target_id="restart-user",
    effective_group_id="",
)
claim = asyncio.run(idempotency.claim_execution(binding, ttl_seconds=300, lease_seconds=60))
print("RESULT=" + json.dumps({"action": claim.action, "result": claim.record.result if claim.record else None}))
"""

    result = run_child(script, {"TEST_DB_PATH": str(database)})

    assert result == {"action": "cached", "result": {"ok": True, "value": "persisted"}}


def test_audit_fingerprint_and_sequence_survive_process_restart(tmp_path, monkeypatch) -> None:
    database = tmp_path / "audit.db"
    monkeypatch.setattr(audit, "DB_PATH", database)
    monkeypatch.setenv(audit.AUDIT_HMAC_KEY_ENV, PERSISTENT_KEY)
    monkeypatch.setattr(audit, "_fingerprint_key", None)
    monkeypatch.setattr(audit, "_fingerprint_key_source", "")
    monkeypatch.setattr(audit, "_fingerprint_key_epoch", "")
    invocation_id = "restart-audit-invocation"
    first_fingerprint = audit.fingerprint_arguments({"value": "same"})
    asyncio.run(
        audit.append_event(
            invocation_id=invocation_id,
            event_type="tool_requested",
            tool_name="restart_audit",
            arguments_fingerprint=first_fingerprint,
        )
    )
    script = """
import asyncio, json, os
from pathlib import Path
import nonebot
nonebot.init()
from plugins.agent_tools import audit
audit.DB_PATH = Path(os.environ["TEST_DB_PATH"])
fingerprint = audit.fingerprint_arguments({"value": "same"})
event = asyncio.run(audit.append_event(
    invocation_id="restart-audit-invocation",
    event_type="execution_completed",
    tool_name="restart_audit",
    arguments_fingerprint=fingerprint,
    outcome="success",
))
source, epoch = audit.fingerprint_key_info()
print("RESULT=" + json.dumps({"fingerprint": fingerprint, "sequence": event.sequence, "source": source, "epoch": epoch}))
"""
    result = run_child(
        script,
        {
            "TEST_DB_PATH": str(database),
            audit.AUDIT_HMAC_KEY_ENV: PERSISTENT_KEY,
        },
    )

    async def stored_sequences() -> list[int]:
        async with aiosqlite.connect(database) as db:
            cursor = await db.execute(
                "SELECT sequence FROM agent_tool_audit_events WHERE invocation_id = ? ORDER BY sequence",
                (invocation_id,),
            )
            return [int(row[0]) for row in await cursor.fetchall()]

    assert result["fingerprint"] == first_fingerprint
    assert result["sequence"] == 2
    assert result["source"] == "persistent"
    assert result["epoch"] == audit.fingerprint_key_info()[1]
    assert asyncio.run(stored_sequences()) == [1, 2]
