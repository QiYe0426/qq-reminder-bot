from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from plugins.agent_tools import audit


KEY_A = "0123456789abcdef" * 4
KEY_B = "fedcba9876543210" * 4


def reset_key(monkeypatch, value: str | None) -> None:
    monkeypatch.setattr(audit, "_fingerprint_key", None)
    monkeypatch.setattr(audit, "_fingerprint_key_source", "")
    monkeypatch.setattr(audit, "_fingerprint_key_epoch", "")
    if value is None:
        monkeypatch.delenv(audit.AUDIT_HMAC_KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(audit.AUDIT_HMAC_KEY_ENV, value)


def subprocess_key_result(key: str) -> dict[str, str]:
    script = """
import json
import nonebot
nonebot.init()
from plugins.agent_tools import audit
source, epoch = audit.fingerprint_key_info()
fingerprint = audit.fingerprint_arguments({"value": "same-payload"})
print("RESULT=" + json.dumps({"source": source, "epoch": epoch, "fingerprint": fingerprint}))
"""
    environment = os.environ.copy()
    environment[audit.AUDIT_HMAC_KEY_ENV] = key
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.getcwd(),
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    result_line = next(line for line in completed.stdout.splitlines() if line.startswith("RESULT="))
    return json.loads(result_line.removeprefix("RESULT="))


def test_same_persistent_key_produces_same_fingerprint_across_instances() -> None:
    first = subprocess_key_result(KEY_A)
    second = subprocess_key_result(KEY_A)

    assert first["source"] == second["source"] == "persistent"
    assert first["fingerprint"] == second["fingerprint"]
    assert first["epoch"] == second["epoch"]


def test_different_keys_produce_different_fingerprints() -> None:
    assert subprocess_key_result(KEY_A)["fingerprint"] != subprocess_key_result(KEY_B)["fingerprint"]


def test_same_and_different_keys_produce_expected_epochs(monkeypatch) -> None:
    reset_key(monkeypatch, KEY_A)
    first = audit.fingerprint_key_info()[1]
    reset_key(monkeypatch, KEY_A)
    same = audit.fingerprint_key_info()[1]
    reset_key(monkeypatch, KEY_B)
    different = audit.fingerprint_key_info()[1]

    assert first == same
    assert first != different
    assert len(first) == audit.AUDIT_HMAC_KEY_EPOCH_LENGTH


def test_missing_key_uses_ephemeral_source_and_emits_warning(monkeypatch) -> None:
    reset_key(monkeypatch, None)
    messages: list[str] = []
    monkeypatch.setattr(
        audit.logger,
        "warning",
        lambda message, *args: messages.append(str(message).format(*args)),
    )

    source, epoch = audit.fingerprint_key_info()

    assert source == "ephemeral"
    assert len(epoch) == audit.AUDIT_HMAC_KEY_EPOCH_LENGTH
    assert len(messages) == 1
    assert "fingerprints_cannot_cross_restarts=true" in messages[0]


def test_invalid_persistent_key_is_rejected_and_not_downgraded(monkeypatch) -> None:
    reset_key(monkeypatch, "not-a-valid-persistent-key")

    with pytest.raises(audit.AuditHmacKeyConfigurationError, match="64 hexadecimal"):
        audit.fingerprint_key_info()

    assert audit._fingerprint_key is None
    assert audit._fingerprint_key_source == ""


def test_key_value_is_not_exposed_in_logs(monkeypatch) -> None:
    reset_key(monkeypatch, KEY_A)
    messages: list[str] = []
    monkeypatch.setattr(
        audit.logger,
        "info",
        lambda message, *args: messages.append(str(message).format(*args)),
    )

    source, epoch = audit.fingerprint_key_info()
    rendered = "\n".join(messages)

    assert source == "persistent"
    assert epoch in rendered
    assert KEY_A not in rendered
    assert audit._fingerprint_key.hex() not in rendered
