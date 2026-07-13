from __future__ import annotations

import os
import stat

import pytest

from plugins.agent_tools import sensitive_storage


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics required")
def test_new_sensitive_directory_uses_restrictive_mode(tmp_path) -> None:
    database = tmp_path / "sensitive" / "audit.db"

    sensitive_storage.prepare_sensitive_sqlite_path(database)

    assert stat.S_IMODE(database.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics required")
def test_new_audit_db_uses_restrictive_mode_with_umask(tmp_path) -> None:
    database = tmp_path / "sensitive" / "audit.db"
    previous_umask = os.umask(0o077)
    try:
        sensitive_storage.prepare_sensitive_sqlite_path(database)
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(database.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics required")
def test_existing_overly_permissive_db_emits_warning(tmp_path, monkeypatch) -> None:
    database = tmp_path / "sensitive" / "audit.db"
    database.parent.mkdir(mode=0o755)
    database.touch(mode=0o644)
    os.chmod(database.parent, 0o755)
    os.chmod(database, 0o644)
    messages: list[str] = []
    monkeypatch.setattr(
        sensitive_storage.logger,
        "warning",
        lambda message, *args: messages.append(str(message).format(*args)),
    )
    monkeypatch.setattr(sensitive_storage, "_warned_permissions", set())

    sensitive_storage.prepare_sensitive_sqlite_path(database)

    assert any("mode=0755" in message for message in messages)
    assert any("mode=0644" in message for message in messages)
    assert stat.S_IMODE(database.stat().st_mode) == 0o644


def test_permission_check_does_not_fail_on_unsupported_platform(tmp_path, monkeypatch) -> None:
    database = tmp_path / "sensitive" / "audit.db"
    monkeypatch.setattr(sensitive_storage, "supports_posix_permissions", lambda: False)

    sensitive_storage.prepare_sensitive_sqlite_path(database)

    assert database.parent.is_dir()
