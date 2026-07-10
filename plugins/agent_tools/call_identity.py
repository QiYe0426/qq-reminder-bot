from __future__ import annotations

import hashlib
import json


IDEMPOTENCY_KEY_VERSION = "v1"


def canonical_arguments(arguments: dict[str, object]) -> str:
    return json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def arguments_digest(arguments: dict[str, object]) -> str:
    return hashlib.sha256(canonical_arguments(arguments).encode("utf-8")).hexdigest()


def build_idempotency_key(
    *,
    tool_name: str,
    arguments: dict[str, object],
    user_id: str,
    target_type: str,
    target_id: str,
    effective_group_id: str,
) -> str:
    identity = {
        "version": IDEMPOTENCY_KEY_VERSION,
        "tool_name": str(tool_name),
        "arguments": arguments,
        "user_id": str(user_id),
        "target_type": str(target_type),
        "target_id": str(target_id),
        "effective_group_id": str(effective_group_id),
    }
    payload = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
