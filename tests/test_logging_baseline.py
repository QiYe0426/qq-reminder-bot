from __future__ import annotations

import ast
import re
from pathlib import Path

from nonebot.log import logger

from plugins import logging_privacy


LOGGER_METHODS = {"debug", "info", "success", "warning", "error", "exception", "critical"}
OLD_STYLE_PLACEHOLDER = re.compile(r"(?<!%)%(?:[#0 +\-]?\d*(?:\.\d+)?)?[sdrfx]")


def old_style_logger_calls(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    findings: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in LOGGER_METHODS or not node.args:
            continue
        template = node.args[0]
        if isinstance(template, ast.Constant) and isinstance(template.value, str):
            if OLD_STYLE_PLACEHOLDER.search(template.value):
                findings.append(node.lineno)
    return findings


def test_production_logger_calls_do_not_use_old_style_placeholders() -> None:
    root = Path(__file__).resolve().parents[1]
    paths = [root / "bot.py", *sorted((root / "plugins").rglob("*.py")), *sorted((root / "scripts").rglob("*.py"))]
    findings = {
        str(path.relative_to(root)): old_style_logger_calls(path)
        for path in paths
        if old_style_logger_calls(path)
    }

    assert findings == {}


def test_safe_mode_redacts_nonebot_event_identifiers_body_and_url(monkeypatch) -> None:
    monkeypatch.setenv(logging_privacy.LOG_PRIVACY_MODE_ENV, "safe")
    original = (
        "<m>OneBot V11 1467807354</m> | [message.group.normal]: "
        "Message 1214181173 from 2647742946@[群:722290838] "
        "'private body [image:url=https://example.test/image?token=secret]'"
    )
    record: dict[str, object] = {"message": original}

    logging_privacy.redact_production_event_log(record)
    rendered = str(record["message"])

    assert "message.group.normal" in rendered
    assert "event_payload_redacted=true" in rendered
    assert "media_segments=1" in rendered
    for sensitive in ("1467807354", "1214181173", "2647742946", "722290838", "private body", "token=secret"):
        assert sensitive not in rendered


def test_debug_mode_requires_explicit_opt_in_and_preserves_event_log(monkeypatch) -> None:
    monkeypatch.setenv(logging_privacy.LOG_PRIVACY_MODE_ENV, "debug")
    original = "<m>OneBot V11 1</m> | [message.private.friend]: Message 2 from 3 'debug body'"
    record: dict[str, object] = {"message": original}

    logging_privacy.redact_production_event_log(record)

    assert record["message"] == original


def test_safe_mode_redacts_real_loguru_color_record(monkeypatch) -> None:
    monkeypatch.setenv(logging_privacy.LOG_PRIVACY_MODE_ENV, "safe")
    rendered: list[str] = []

    def capture(record) -> bool:
        logging_privacy.redact_production_event_log(record)
        rendered.append(str(record["message"]))
        return False

    sink_id = logger.add(lambda _: None, filter=capture)
    try:
        logger.opt(colors=True).success(
            "<m>OneBot V11 1467807354</m> | [message.group.normal]: "
            "Message 1214181173 from 2647742946@[群:722290838] "
            "'private body [image:url=https://example.test/image?token=secret]'"
        )
    finally:
        logger.remove(sink_id)

    assert len(rendered) == 1
    assert "event_payload_redacted=true" in rendered[0]
    for sensitive in ("1467807354", "1214181173", "2647742946", "722290838", "private body", "token=secret"):
        assert sensitive not in rendered[0]
