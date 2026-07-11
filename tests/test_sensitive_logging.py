from __future__ import annotations

import json

from plugins.sensitive_logging import log_fingerprint, safe_text_log_details, safe_url_log_details


def serialized(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def test_qq_and_group_ids_are_replaced_by_fingerprints() -> None:
    user_id = "123456789"
    group_id = "987654321"
    details = {
        "actor_fingerprint": log_fingerprint("user_id", user_id),
        "scope_fingerprint": log_fingerprint("group_id", group_id),
    }
    output = serialized(details)

    assert user_id not in output
    assert group_id not in output
    assert details["actor_fingerprint"] != details["scope_fingerprint"]


def test_url_query_userinfo_and_fragment_are_not_logged() -> None:
    url = "https://alice:secret@example.com/path?q=private-search#private-fragment"
    output = serialized(safe_url_log_details(url))

    assert "https://example.com/path" in output
    assert "alice" not in output
    assert "secret" not in output
    assert "private-search" not in output
    assert "private-fragment" not in output


def test_reminder_body_is_replaced_by_length_and_fingerprint() -> None:
    reminder_body = "提醒我明天把秘密合同发给小王"
    details = safe_text_log_details("reminder_body", reminder_body)
    output = serialized(details)

    assert reminder_body not in output
    assert details["text_length"] == len(reminder_body)
    assert details["text_fingerprint"]


def test_user_input_and_search_keywords_are_not_logged() -> None:
    user_input = "帮我搜索不能出现在日志里的私人关键词"
    details = safe_text_log_details("user_input", user_input)
    output = serialized(details)

    assert user_input not in output
    assert "私人关键词" not in output
    assert details["text_length"] == len(user_input)
