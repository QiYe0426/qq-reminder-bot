import nonebot


nonebot.init()

from plugins import admin_console  # noqa: E402


def test_admin_session_changes_with_token(monkeypatch) -> None:
    monkeypatch.setenv(admin_console.ADMIN_TOKEN_ENV, "first-admin-token")
    first = admin_console.admin_session_value()

    assert len(first) == 64
    assert admin_console.valid_admin_session(first)
    assert not admin_console.valid_admin_session("invalid")

    monkeypatch.setenv(admin_console.ADMIN_TOKEN_ENV, "second-admin-token")
    assert not admin_console.valid_admin_session(first)


def test_cookie_from_scope() -> None:
    scope = {
        "headers": [
            (b"host", b"example.test"),
            (
                b"cookie",
                b"other=value; hunterbot_admin_session=session-value",
            ),
        ]
    }

    assert (
        admin_console.cookie_from_scope(scope, admin_console.ADMIN_SESSION_COOKIE)
        == "session-value"
    )
