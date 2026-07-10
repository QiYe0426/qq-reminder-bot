from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_agent_tool_execution_db(tmp_path, monkeypatch) -> None:
    from plugins.agent_tools import idempotency

    monkeypatch.setattr(idempotency, "DB_PATH", tmp_path / "agent_tool_executions.db")
