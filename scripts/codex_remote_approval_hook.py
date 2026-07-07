from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import socket
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


DEFAULT_API_BASE = "http://127.0.0.1:8090/hunterbot/remote-approval/api"
DEFAULT_HTTP_TIMEOUT_SECONDS = 30
DEFAULT_POLL_SECONDS = 2
DEFAULT_CONNECT_RETRY_SECONDS = 5

SENSITIVE_KEY_RE = re.compile(
    r"(api[-_]?key|token|secret|password|passwd|pwd|authorization|cookie|session|credential|private[-_]?key|bearer)",
    re.IGNORECASE,
)
URL_SECRET_RE = re.compile(
    r"([?&][^=&\s]*(?:token|key|secret|password|auth|cookie|session)[^=&\s]*=)[^&\s]+",
    re.IGNORECASE,
)
ENV_SECRET_RE = re.compile(
    r"\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|AUTH|COOKIE|SESSION)[A-Z0-9_]*\s*=\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s;&|]+)",
    re.IGNORECASE,
)
BEARER_RE = re.compile(r"\b(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
LONG_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])([A-Za-z0-9_-]{32,})(?![A-Za-z0-9_-])")
WINDOWS_USER_RE = re.compile(r"([A-Za-z]:\\Users\\)[^\\\s]+", re.IGNORECASE)

COMMAND_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;|\||\r?\n)\s*")
KNOWN_CMDLETS = re.compile(
    r"\b(?:Remove-Item|Move-Item|Copy-Item|Start-Process|Invoke-WebRequest|Invoke-RestMethod|Set-Content|Add-Content|"
    r"Get-Content|New-Item|ssh|scp|git|npm|pnpm|node|python|py|pip|pytest|cargo|go|dotnet|curl|wget|rm|del|rmdir|mkdir)\b",
    re.IGNORECASE,
)


def eprint(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def script_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists() or not path.is_file():
        return values
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in values:
                values[key] = value
    except OSError:
        return values
    return values


DOTENV_VALUES = {
    **load_env_file(script_root() / ".env.local"),
    **load_env_file(Path.cwd() / ".env.local"),
}


def config_value(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value.strip()
    for name in names:
        value = DOTENV_VALUES.get(name)
        if value:
            return value.strip()
    return default


def config_float(*names: str, default: float) -> float:
    raw = config_value(*names)
    if not raw:
        return default
    try:
        return max(0.1, float(raw))
    except ValueError:
        return default


def api_base_url() -> str:
    base = config_value(
        "CODEX_REMOTE_APPROVAL_URL",
        "REMOTE_APPROVAL_URL",
        "REMOTE_APPROVAL_API_URL",
        default=DEFAULT_API_BASE,
    ).rstrip("/")
    if not base.endswith("/api"):
        base = f"{base}/api"
    return base


def api_token() -> str:
    return config_value("CODEX_REMOTE_APPROVAL_TOKEN", "REMOTE_APPROVAL_API_TOKEN")


def redact_text(text: object, limit: int = 700) -> str:
    value = str(text or "")
    value = URL_SECRET_RE.sub(r"\1[REDACTED]", value)
    value = ENV_SECRET_RE.sub(r"\1[REDACTED]", value)
    value = BEARER_RE.sub(r"\1[REDACTED]", value)
    value = WINDOWS_USER_RE.sub(r"\1[USER]", value)
    value = LONG_TOKEN_RE.sub("[REDACTED]", value)
    value = " ".join(value.split()).strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def redact_data(value: object, key: str = "") -> object:
    if SENSITIVE_KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact_data(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_data(item, key) for item in value[:20]]
    if isinstance(value, str):
        return redact_text(value)
    return value


def shell_tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=False)
    except ValueError:
        return re.findall(r"[^\s]+", segment)


def simplify_token(token: str) -> str:
    value = token.strip().strip('"').strip("'")
    if "\\" in value or "/" in value:
        value = value.replace("\\", "/").rsplit("/", 1)[-1]
    return value


def segment_command_head(segment: str) -> str | None:
    cleaned = redact_text(segment, limit=500).strip()
    if not cleaned:
        return None

    cmdlet_matches = KNOWN_CMDLETS.findall(cleaned)
    if cmdlet_matches:
        unique: list[str] = []
        for item in cmdlet_matches:
            normalized = simplify_token(item)
            if normalized.lower() not in {known.lower() for known in unique}:
                unique.append(normalized)
        return " + ".join(unique[:3])

    tokens = shell_tokens(cleaned)
    filtered: list[str] = []
    for token in tokens:
        token = simplify_token(token)
        if not token or token in {"&", "cmd", "/c", "-Command", "-c"}:
            continue
        if "=" in token and not token.startswith("-"):
            continue
        filtered.append(token)
    if not filtered:
        return None

    command = filtered[0]
    include_next_for = {"git", "npm", "pnpm", "node", "python", "py", "pip", "pytest", "cargo", "go", "dotnet"}
    if command.lower() in include_next_for and len(filtered) > 1 and not filtered[1].startswith("-"):
        return f"{command} {filtered[1]}"
    return command


def summarize_patch(command: str) -> str:
    files: list[str] = []
    added = 0
    removed = 0
    operations: list[str] = []
    for line in command.splitlines():
        if line.startswith("*** Add File: "):
            operations.append("add")
            files.append(line.removeprefix("*** Add File: ").strip())
        elif line.startswith("*** Update File: "):
            operations.append("update")
            files.append(line.removeprefix("*** Update File: ").strip())
        elif line.startswith("*** Delete File: "):
            operations.append("delete")
            files.append(line.removeprefix("*** Delete File: ").strip())
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    unique_files: list[str] = []
    for file_name in files:
        file_name = redact_text(file_name, limit=160)
        if file_name and file_name not in unique_files:
            unique_files.append(file_name)
    op_text = "/".join(sorted(set(operations))) or "patch"
    file_text = ", ".join(unique_files[:6]) if unique_files else "unknown files"
    if len(unique_files) > 6:
        file_text += f", +{len(unique_files) - 6} more"
    return f"patch {op_text}; files: {file_text}; line changes: +{added}/-{removed}"


def summarize_shell_command(command: str) -> str:
    if command.lstrip().startswith("*** Begin Patch"):
        return summarize_patch(command)
    heads: list[str] = []
    for segment in COMMAND_SPLIT_RE.split(command):
        head = segment_command_head(segment)
        if head and head.lower() not in {item.lower() for item in heads}:
            heads.append(head)
    if heads:
        return f"shell command summary: {' | '.join(heads[:5])}; arguments hidden/redacted"
    return "shell command requested; full command hidden"


def summarize_mcp_input(tool_input: object) -> str:
    safe_input = redact_data(tool_input)
    if isinstance(safe_input, dict):
        keys = ", ".join(sorted(str(key) for key in safe_input.keys())[:10])
        return f"MCP/tool request with fields: {keys or 'none'}"
    if isinstance(safe_input, list):
        return f"MCP/tool request with list input length {len(safe_input)}"
    return f"MCP/tool request input type: {type(safe_input).__name__}"


def find_first_string(value: object, keys: set[str]) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in keys and isinstance(item, str) and item.strip():
                return item
        for item in value.values():
            result = find_first_string(item, keys)
            if result:
                return result
    elif isinstance(value, list):
        for item in value:
            result = find_first_string(item, keys)
            if result:
                return result
    return ""


def risk_summary(command: str, tool_name: str, event: dict[str, object]) -> str:
    haystack = f"{tool_name}\n{command}\n{json.dumps(event, ensure_ascii=False, default=str)}".lower()
    risks: list[str] = []
    if "require_escalated" in haystack or "escalat" in haystack:
        risks.append("requests elevated permission")
    if re.search(r"\b(remove-item|rm|del|rmdir|git\s+reset|git\s+clean)\b", haystack):
        risks.append("may delete or reset files")
    if re.search(r"\b(invoke-webrequest|invoke-restmethod|curl|wget|ssh|scp|git\s+pull|git\s+push)\b", haystack):
        risks.append("uses network or remote access")
    if "danger-full-access" in haystack or "bypasspermissions" in haystack:
        risks.append("runs with broad local access")
    return "; ".join(risks) if risks else "normal approval-required operation"


def build_summary(event: dict[str, object], raw_input: str) -> dict[str, str]:
    tool_name = str(event.get("tool_name") or event.get("tool") or "")
    hook_name = str(event.get("hook_event_name") or "PermissionRequest")
    cwd = str(event.get("cwd") or "")
    tool_input = event.get("tool_input")
    command = ""
    description = ""
    if isinstance(tool_input, dict):
        command = str(tool_input.get("command") or "")
        description = str(tool_input.get("description") or "")
    if not command:
        command = find_first_string(event, {"command", "cmd", "script"})
    if not description:
        description = find_first_string(event, {"description", "justification", "reason"})

    if command:
        operation_summary = summarize_shell_command(command)
    else:
        operation_summary = summarize_mcp_input(tool_input)

    requester = config_value("CODEX_REMOTE_APPROVAL_REQUESTER")
    if not requester:
        requester = f"{os.getenv('USERNAME') or os.getenv('USER') or 'unknown'}@{socket.gethostname()}"

    digest = hashlib.sha256(raw_input.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return {
        "tool_name": redact_text(tool_name or "unknown", limit=120),
        "hook_event_name": redact_text(hook_name, limit=80),
        "cwd_summary": redact_text(cwd, limit=260),
        "operation_summary": redact_text(operation_summary, limit=700),
        "reason_summary": redact_text(description, limit=500),
        "risk_summary": redact_text(risk_summary(command, tool_name, event), limit=300),
        "requester": redact_text(requester, limit=120),
        "payload_digest": digest,
    }


def http_json(method: str, url: str, token: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    data = None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "codex-remote-approval-hook/1.0",
    }
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    timeout = config_float("CODEX_REMOTE_APPROVAL_HTTP_TIMEOUT_SECONDS", default=DEFAULT_HTTP_TIMEOUT_SECONDS)
    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
    parsed = json.loads(body or "{}")
    if not isinstance(parsed, dict):
        raise RuntimeError("remote approval API returned non-object JSON")
    return parsed


def permission_decision(behavior: str, message: str = "") -> dict[str, object]:
    decision: dict[str, object] = {"behavior": behavior}
    if message:
        decision["message"] = message
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": decision,
        }
    }


def emit_permission_decision(behavior: str, message: str = "") -> None:
    print(json.dumps(permission_decision(behavior, message), ensure_ascii=False))


def create_remote_request(base_url: str, token: str, payload: dict[str, str]) -> dict[str, object]:
    retry_seconds = config_float(
        "CODEX_REMOTE_APPROVAL_CONNECT_RETRY_SECONDS",
        default=DEFAULT_CONNECT_RETRY_SECONDS,
    )
    create_url = urljoin(f"{base_url}/", "requests")
    while True:
        try:
            response = http_json("POST", create_url, token, payload)
            request_value = response.get("request")
            if not isinstance(request_value, dict):
                raise RuntimeError("remote approval API response did not include request")
            return request_value
        except HTTPError as exc:
            if exc.code in {400, 401, 403, 404}:
                raise RuntimeError(f"remote approval API rejected request with HTTP {exc.code}") from exc
            eprint(f"Remote approval API HTTP {exc.code}; retrying in {retry_seconds:g}s.")
        except (URLError, TimeoutError, OSError, json.JSONDecodeError, RuntimeError) as exc:
            eprint(f"Remote approval API unavailable: {exc}; retrying in {retry_seconds:g}s.")
        time.sleep(retry_seconds)


def wait_for_decision(base_url: str, token: str, request_id: str) -> dict[str, object]:
    poll_seconds = config_float("CODEX_REMOTE_APPROVAL_POLL_SECONDS", default=DEFAULT_POLL_SECONDS)
    wait_url = urljoin(f"{base_url}/", f"requests/{request_id}/wait?wait_seconds=25")
    while True:
        try:
            response = http_json("GET", wait_url, token)
            request_value = response.get("request")
            if not isinstance(request_value, dict):
                raise RuntimeError("remote approval API response did not include request")
            status = str(request_value.get("status") or "")
            if status != "pending":
                return request_value
        except HTTPError as exc:
            if exc.code in {401, 403, 404}:
                raise RuntimeError(f"remote approval wait failed with HTTP {exc.code}") from exc
            eprint(f"Remote approval wait HTTP {exc.code}; retrying in {poll_seconds:g}s.")
        except (URLError, TimeoutError, OSError, json.JSONDecodeError, RuntimeError) as exc:
            eprint(f"Remote approval wait unavailable: {exc}; retrying in {poll_seconds:g}s.")
        time.sleep(poll_seconds)


def main() -> int:
    raw_input = sys.stdin.read()
    try:
        event = json.loads(raw_input or "{}")
    except json.JSONDecodeError:
        event = {"tool_input": {"description": "Invalid JSON hook input", "command": raw_input}}
    if not isinstance(event, dict):
        event = {"tool_input": {"description": "Non-object hook input", "command": raw_input}}

    token = api_token()
    if not token:
        emit_permission_decision("deny", "Remote approval token is not configured.")
        return 0

    base_url = api_base_url()
    payload = build_summary(event, raw_input)

    try:
        request_value = create_remote_request(base_url, token, payload)
        code = str(request_value.get("approval_code") or "")
        request_id = str(request_value.get("request_id") or "")
        if not request_id:
            raise RuntimeError("remote approval request id is missing")
        if code:
            eprint(f"Waiting for QQ remote approval code {code}.")
        decision = wait_for_decision(base_url, token, request_id)
    except Exception as exc:
        emit_permission_decision("deny", f"Remote approval failed: {exc}")
        return 0

    status = str(decision.get("status") or "")
    code = str(decision.get("approval_code") or "")
    if status == "approved":
        emit_permission_decision("allow", f"Approved by QQ remote approval {code}.")
    elif status == "cancelled":
        emit_permission_decision("deny", f"Remote approval {code} was cancelled from QQ.")
    else:
        emit_permission_decision("deny", f"Remote approval {code} was denied from QQ.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
