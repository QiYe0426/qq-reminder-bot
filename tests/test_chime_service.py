from datetime import datetime

from plugins.chime_service import (
    CHIME_MODE_HOURLY,
    CHIME_MODE_TWICE_DAILY,
    format_chime_time,
    parse_chime_mode,
    should_send_chime,
)


def test_parse_chime_aliases() -> None:
    assert parse_chime_mode("开启🎒常数报时") == CHIME_MODE_HOURLY
    assert parse_chime_mode("启用🎒常数报时 每天两次") == CHIME_MODE_TWICE_DAILY
    assert parse_chime_mode("不存在的模式") is None


def test_twice_daily_chime_hours() -> None:
    assert should_send_chime(CHIME_MODE_TWICE_DAILY, datetime(2026, 7, 7, 1, 58))
    assert should_send_chime(CHIME_MODE_TWICE_DAILY, datetime(2026, 7, 7, 13, 58))
    assert not should_send_chime(CHIME_MODE_TWICE_DAILY, datetime(2026, 7, 7, 12, 58))


def test_format_chime_time() -> None:
    assert format_chime_time(datetime(2026, 7, 7, 0, 58)) == "凌晨12:58"
    assert format_chime_time(datetime(2026, 7, 7, 13, 58)) == "下午1:58"
