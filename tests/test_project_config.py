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

    assert config["project"]["version"] == "2.4.0"
    assert "## v2.4.0" in (ROOT / "VERSION.md").read_text(encoding="utf-8")


def test_required_admin_console_assets_exist() -> None:
    static_dir = ROOT / "plugins" / "admin_console" / "static"
    assert (static_dir / "index.html").is_file()
    assert (static_dir / "app.js").is_file()
    assert (static_dir / "style.css").is_file()


def test_local_ocr_configuration_replaces_cloud_vision_configuration() -> None:
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in (
        "LOCAL_OCR_ENABLED",
        "LOCAL_OCR_CONFIDENCE",
        "LOCAL_OCR_TIMEOUT_SECONDS",
        "LOCAL_OCR_MAX_IMAGE_BYTES",
        "LOCAL_OCR_CACHE_TTL_SECONDS",
        "LOCAL_OCR_CACHE_MAX_ENTRIES",
        "LOCAL_OCR_CONCURRENCY",
    ):
        assert f"{name}=" in env_example
    for name in (
        "IMAGE_VISION_MODEL",
        "IMAGE_VISION_API_KEY",
        "IMAGE_VISION_BASE_URL",
        "KEYWORD_RETORT_IMAGE_SCAN_MODEL",
        "KEYWORD_RETORT_IMAGE_SCAN_API_KEY",
        "KEYWORD_RETORT_IMAGE_SCAN_BASE_URL",
    ):
        assert f"{name}=" not in env_example
