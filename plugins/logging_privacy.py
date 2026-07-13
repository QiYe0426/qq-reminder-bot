from __future__ import annotations

import os
import re
import sys
from collections.abc import MutableMapping

from nonebot.log import default_filter, default_format, logger, logger_id


LOG_PRIVACY_MODE_ENV = "LOG_PRIVACY_MODE"
LOG_PRIVACY_MODE_SAFE = "safe"
LOG_PRIVACY_MODE_DEBUG = "debug"

_ONEBOT_MESSAGE_EVENT = re.compile(
    r"^(?:<m>)?(?P<adapter>OneBot V11)\s+[^<|]+(?:</m>)?\s+\|\s+"
    r"(?P<event>\[[^\]]+\]):\s+Message\s+(?P<payload>.*)$",
    re.DOTALL,
)
_MEDIA_SEGMENT = re.compile(r"\[(?:image|record|video|file)(?=[:\],])", re.IGNORECASE)


def log_privacy_mode() -> str:
    configured = os.getenv(LOG_PRIVACY_MODE_ENV, LOG_PRIVACY_MODE_SAFE).strip().lower()
    return configured if configured in {LOG_PRIVACY_MODE_SAFE, LOG_PRIVACY_MODE_DEBUG} else LOG_PRIVACY_MODE_SAFE


def redact_production_event_log(record: MutableMapping[str, object]) -> None:
    """Remove OneBot message identifiers, bodies, and media URLs before output."""

    if log_privacy_mode() == LOG_PRIVACY_MODE_DEBUG:
        return
    message = str(record.get("message") or "")
    match = _ONEBOT_MESSAGE_EVENT.fullmatch(message)
    if match is None:
        return
    payload = match.group("payload")
    record["message"] = (
        f"{match.group('adapter')} | {match.group('event')}: "
        f"event_payload_redacted=true event_size_chars={len(payload)} "
        f"media_segments={len(_MEDIA_SEGMENT.findall(payload))}"
    )


def _privacy_filter(record) -> bool:
    redact_production_event_log(record)
    return bool(default_filter(record))


def configure_log_privacy() -> None:
    """Replace the default stdout sink with an equivalent privacy-aware sink."""

    logger.remove(logger_id)
    logger.add(
        sys.stdout,
        level=0,
        diagnose=False,
        filter=_privacy_filter,
        format=default_format,
    )
    configured = os.getenv(LOG_PRIVACY_MODE_ENV, LOG_PRIVACY_MODE_SAFE).strip().lower()
    if configured and configured not in {LOG_PRIVACY_MODE_SAFE, LOG_PRIVACY_MODE_DEBUG}:
        logger.warning(
            "Invalid log privacy mode; using safe mode: env={} configured_length={}",
            LOG_PRIVACY_MODE_ENV,
            len(configured),
        )
    logger.info("Log privacy initialized: mode={}", log_privacy_mode())
