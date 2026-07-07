from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_configured_plugins_have_source_files() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)

    plugins = config["tool"]["nonebot"]["plugins"]
    assert "plugins.remote_approval" in plugins

    missing = []
    for plugin in plugins:
        relative = Path(*plugin.split("."))
        module_file = ROOT / relative.with_suffix(".py")
        package_file = ROOT / relative / "__init__.py"
        if not module_file.is_file() and not package_file.is_file():
            missing.append(plugin)

    assert missing == []


def test_release_version_is_consistent() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)

    assert config["project"]["version"] == "2.2.3"
    assert "## v2.2.0" in (ROOT / "VERSION.md").read_text(encoding="utf-8")


def test_required_admin_console_assets_exist() -> None:
    static_dir = ROOT / "plugins" / "admin_console" / "static"
    assert (static_dir / "index.html").is_file()
    assert (static_dir / "app.js").is_file()
    assert (static_dir / "style.css").is_file()
