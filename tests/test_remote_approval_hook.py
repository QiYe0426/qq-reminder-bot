import importlib.util
from pathlib import Path


HOOK_PATH = Path(__file__).resolve().parents[1] / "scripts" / "codex_remote_approval_hook.py"
SPEC = importlib.util.spec_from_file_location("codex_remote_approval_hook", HOOK_PATH)
assert SPEC is not None and SPEC.loader is not None
HOOK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOOK)


def test_redact_text_hides_credentials_and_user_name() -> None:
    text = (
        "TOKEN=very-secret-token-value "
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345 "
        r"C:\Users\alice\project"
    )

    result = HOOK.redact_text(text)

    assert "very-secret-token-value" not in result
    assert "abcdefghijklmnopqrstuvwxyz012345" not in result
    assert r"C:\Users\alice" not in result
    assert "[REDACTED]" in result
    assert r"C:\Users\[USER]" in result


def test_shell_summary_keeps_command_names_but_hides_arguments() -> None:
    result = HOOK.summarize_shell_command("git push origin main && python secret.py --token abc")

    assert "git" in result
    assert "python" in result
    assert "--token" not in result
    assert "abc" not in result
