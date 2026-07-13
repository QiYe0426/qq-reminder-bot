from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_agent_tool_execution_db(tmp_path, monkeypatch) -> None:
    from plugins.agent_tools import audit, idempotency

    monkeypatch.setattr(idempotency, "DB_PATH", tmp_path / "agent_tool_executions.db")
    monkeypatch.setattr(audit, "DB_PATH", tmp_path / "agent_tool_audit.db")
    monkeypatch.setattr(audit, "_fingerprint_key", b"a" * 64)
    monkeypatch.setattr(audit, "_fingerprint_key_source", "test")
    monkeypatch.setattr(audit, "_fingerprint_key_epoch", "test-epoch")
