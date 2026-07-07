import asyncio

from plugins import access_control
from plugins.access_control import (
    FEATURE_COLLECTOR,
    FEATURE_COMPANION,
    FEATURE_DAILY_REPORT,
    FEATURE_DAILY_REPORT_AUTO,
    enforce_group_feature_dependencies,
    init_access_db,
    is_group_feature_enabled,
    normalize_feature_name,
    set_group_feature,
)


def reset_access_db(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(access_control, "DB_PATH", tmp_path / "bot_settings.db")
    monkeypatch.setattr(access_control, "_db_ready", False)


def test_daily_report_alias_is_separate_from_collector() -> None:
    assert normalize_feature_name("消息采集") == FEATURE_COLLECTOR
    assert normalize_feature_name("日报") == FEATURE_DAILY_REPORT
    assert normalize_feature_name("日报功能") == FEATURE_DAILY_REPORT


def test_old_collector_enabled_groups_migrate_to_daily_report(tmp_path, monkeypatch) -> None:
    reset_access_db(tmp_path, monkeypatch)

    async def run() -> None:
        await init_access_db()
        await set_group_feature("1001", FEATURE_COLLECTOR, True)

        access_control._db_ready = False
        await init_access_db()

        assert await is_group_feature_enabled("1001", FEATURE_COLLECTOR)
        assert await is_group_feature_enabled("1001", FEATURE_DAILY_REPORT)

    asyncio.run(run())


def test_collector_off_disables_dependent_features(tmp_path, monkeypatch) -> None:
    reset_access_db(tmp_path, monkeypatch)

    async def run() -> None:
        group_id = "1002"
        await init_access_db()
        await set_group_feature(group_id, FEATURE_COLLECTOR, True)
        await set_group_feature(group_id, FEATURE_DAILY_REPORT, True)
        await set_group_feature(group_id, FEATURE_DAILY_REPORT_AUTO, True)
        await set_group_feature(group_id, FEATURE_COMPANION, True)

        await set_group_feature(group_id, FEATURE_COLLECTOR, False)
        await enforce_group_feature_dependencies(group_id)

        assert not await is_group_feature_enabled(group_id, FEATURE_COLLECTOR)
        assert not await is_group_feature_enabled(group_id, FEATURE_DAILY_REPORT)
        assert not await is_group_feature_enabled(group_id, FEATURE_DAILY_REPORT_AUTO)
        assert not await is_group_feature_enabled(group_id, FEATURE_COMPANION)

    asyncio.run(run())


def test_daily_report_off_disables_auto_only(tmp_path, monkeypatch) -> None:
    reset_access_db(tmp_path, monkeypatch)

    async def run() -> None:
        group_id = "1003"
        await init_access_db()
        await set_group_feature(group_id, FEATURE_COLLECTOR, True)
        await set_group_feature(group_id, FEATURE_DAILY_REPORT, False)
        await set_group_feature(group_id, FEATURE_DAILY_REPORT_AUTO, True)
        await set_group_feature(group_id, FEATURE_COMPANION, True)

        await enforce_group_feature_dependencies(group_id)

        assert await is_group_feature_enabled(group_id, FEATURE_COLLECTOR)
        assert not await is_group_feature_enabled(group_id, FEATURE_DAILY_REPORT)
        assert not await is_group_feature_enabled(group_id, FEATURE_DAILY_REPORT_AUTO)
        assert await is_group_feature_enabled(group_id, FEATURE_COMPANION)

    asyncio.run(run())
