from datetime import datetime

from plugins.reminder_service import parse_number, parse_reminder


def test_parse_chinese_numbers() -> None:
    assert parse_number("一") == 1
    assert parse_number("十二") == 12
    assert parse_number("一百零五") == 105
    assert parse_number("俩") == 2


def test_parse_relative_reminder() -> None:
    now = datetime(2026, 7, 7, 10, 30)

    parsed = parse_reminder("一天2小时三十分钟后 吃饭", now=now)

    assert parsed == (datetime(2026, 7, 8, 13, 0), "吃饭")


def test_parse_time_only_rolls_to_tomorrow() -> None:
    now = datetime(2026, 7, 7, 10, 30)

    parsed = parse_reminder("09:00 喝水", now=now)

    assert parsed == (datetime(2026, 7, 8, 9, 0), "喝水")


def test_parse_tomorrow_same_time_with_request_prefix() -> None:
    now = datetime(2026, 7, 7, 10, 30, 45)

    parsed = parse_reminder("我要明天这个时候吃饭", now=now)

    assert parsed == (datetime(2026, 7, 8, 10, 30), "吃饭")


def test_parse_tomorrow_same_point() -> None:
    now = datetime(2026, 7, 7, 10, 30)

    parsed = parse_reminder("明天这个点提醒我喝水", now=now)

    assert parsed == (datetime(2026, 7, 8, 10, 30), "喝水")


def test_parse_natural_daypart_time() -> None:
    now = datetime(2026, 7, 7, 10, 30)

    parsed = parse_reminder("明天下午三点开会", now=now)

    assert parsed == (datetime(2026, 7, 8, 15, 0), "开会")


def test_parse_tonight_time() -> None:
    now = datetime(2026, 7, 7, 10, 30)

    parsed = parse_reminder("今晚八点吃饭", now=now)

    assert parsed == (datetime(2026, 7, 7, 20, 0), "吃饭")


def test_parse_natural_day_clock() -> None:
    now = datetime(2026, 7, 7, 10, 30)

    parsed = parse_reminder("明天 09:00 喝水", now=now)

    assert parsed == (datetime(2026, 7, 8, 9, 0), "喝水")


def test_parse_invalid_reminder_returns_none() -> None:
    assert parse_reminder("以后提醒我") is None


def test_parse_relative_reminder_with_generic_remind_me_content() -> None:
    now = datetime(2026, 7, 7, 10, 30)

    parsed = parse_reminder("1分钟后提醒我", now=now)

    assert parsed == (datetime(2026, 7, 7, 10, 31), "提醒我")
