from __future__ import annotations

import os
import stat
from pathlib import Path

from nonebot.log import logger


SENSITIVE_DIRECTORY_MODE = 0o700
SENSITIVE_FILE_MODE = 0o600

_warned_permissions: set[tuple[str, int, int]] = set()


def supports_posix_permissions() -> bool:
    return os.name == "posix"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _warn_if_overly_permissive(path: Path, expected_mode: int) -> None:
    try:
        actual_mode = _mode(path)
    except OSError as exc:
        logger.warning(
            "Unable to inspect sensitive storage permissions: path={} error_type={}",
            path.as_posix(),
            type(exc).__name__,
        )
        return
    if actual_mode & ~expected_mode == 0:
        return
    warning_key = (str(path), actual_mode, expected_mode)
    if warning_key in _warned_permissions:
        return
    _warned_permissions.add(warning_key)
    logger.warning(
        "Sensitive storage permissions are broader than recommended: path={} mode={:04o} recommended={:04o}",
        path.as_posix(),
        actual_mode,
        expected_mode,
    )


def _create_sensitive_file(path: Path) -> bool:
    try:
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            SENSITIVE_FILE_MODE,
        )
    except FileExistsError:
        return False
    try:
        os.close(descriptor)
    finally:
        os.chmod(path, SENSITIVE_FILE_MODE)
    return True


def prepare_sensitive_sqlite_path(path: Path) -> None:
    """Create new sensitive SQLite paths restrictively and audit existing modes.

    Existing permissions are never changed automatically. Platforms without
    POSIX permission semantics keep the previous mkdir/SQLite behavior.
    """

    path = Path(path)
    parent = path.parent
    parent_existed = parent.exists()
    parent.mkdir(parents=True, exist_ok=True, mode=SENSITIVE_DIRECTORY_MODE)
    if not supports_posix_permissions():
        return

    if parent_existed:
        _warn_if_overly_permissive(parent, SENSITIVE_DIRECTORY_MODE)
    else:
        try:
            os.chmod(parent, SENSITIVE_DIRECTORY_MODE)
        except OSError as exc:
            logger.warning(
                "Unable to set sensitive directory permissions: path={} error_type={}",
                parent.as_posix(),
                type(exc).__name__,
            )

    try:
        created = _create_sensitive_file(path) if not path.exists() else False
    except OSError as exc:
        logger.warning(
            "Unable to pre-create sensitive database securely: path={} error_type={}",
            path.as_posix(),
            type(exc).__name__,
        )
        raise
    if not created and path.exists():
        _warn_if_overly_permissive(path, SENSITIVE_FILE_MODE)
