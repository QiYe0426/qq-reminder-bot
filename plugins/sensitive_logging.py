from __future__ import annotations

from plugins.agent_tools.audit import safe_fingerprint, sanitize_details


def log_fingerprint(namespace: str, value: object) -> str:
    """Return a stable, non-reversible identifier suitable for operational logs."""
    return safe_fingerprint(namespace, value)


def safe_url_log_details(url: object) -> dict[str, object]:
    """Return URL metadata with userinfo, query, and fragment removed."""
    return sanitize_details({"url": str(url)})


def safe_text_log_details(namespace: str, value: object) -> dict[str, object]:
    """Describe sensitive free text without retaining its contents."""
    text = str(value)
    return {
        "text_fingerprint": log_fingerprint(namespace, text),
        "text_length": len(text),
    }
