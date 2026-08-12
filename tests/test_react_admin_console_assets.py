from pathlib import Path


STATIC = Path("plugins/admin_console/static")


def test_admin_console_is_built_as_react_application() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    app = (STATIC / "react-app.js").read_text(encoding="utf-8")

    assert '<div id="root"></div>' in index
    assert "react-app.js" in index
    assert "React" in app
    assert "常规群管理" in app
    assert "剧本杀模式" in app


def test_admin_console_has_responsive_styles() -> None:
    css = (STATIC / "react-style.css").read_text(encoding="utf-8")
    assert "@media (max-width: 760px)" in css
    assert ".mobile-nav" in css


def test_admin_console_registers_game_management_routes() -> None:
    source = Path("plugins/admin_console/__init__.py").read_text(encoding="utf-8")
    assert 'f"{ROUTE_PREFIX}/api/games"' in source
    assert 'f"{ROUTE_PREFIX}/api/games/{{game_id}}/control"' in source
    assert "check_token(token=token, authorization=authorization)" in source
