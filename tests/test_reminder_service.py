from datetime import datetime
import asyncio

from plugins import reminder_service
from plugins.reminder_service import (
    ReminderScope,
    ReminderTarget,
    create_reminder,
    due_reminders,
    format_due_reminder_message,
    parse_number,
    parse_reminder,
)


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


def test_group_due_reminder_mentions_creator() -> None:
    message = format_due_reminder_message(
        {
            "user_id": "10001",
            "group_id": "20001",
            "target_type": "group",
            "content": "喝水",
        }
    )

    assert message == "[CQ:at,qq=10001] 提醒：喝水"


def test_private_due_reminder_does_not_mention_user() -> None:
    message = format_due_reminder_message(
        {
            "user_id": "10001",
            "target_type": "private",
            "content": "喝水",
        }
    )

    assert message == "提醒：喝水"


def test_due_reminder_escapes_cq_content() -> None:
    message = format_due_reminder_message(
        {
            "user_id": "10001",
            "group_id": "20001",
            "target_type": "group",
            "content": "[CQ:at,qq=all]集合",
        }
    )

    assert message == "[CQ:at,qq=10001] 提醒：&#91;CQ:at,qq=all&#93;集合"


def test_targeted_group_reminder_mentions_target_user(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(reminder_service, "DB_PATH", tmp_path / "reminders.db")
    monkeypatch.setattr(reminder_service, "_db_ready", False)

    async def run() -> tuple[dict[str, object], list[dict[str, object]]]:
        created = await create_reminder(
            ReminderScope(user_id="10001", target_type="group", group_id="20001"),
            "2099-01-01 09:00 提醒小明喝水",
            target=ReminderTarget(user_id="10002", display_name="小明"),
            content_override="喝水",
        )
        reminders = await due_reminders(now=datetime(2099, 1, 1, 9, 0))
        return created, reminders

    created, reminders = asyncio.run(run())

    assert created["ok"] is True
    assert created["target_user_id"] == "10002"
    assert created["target_display_name"] == "小明"
    assert created["message"] == "已创建提醒 #1：2099-01-01 09:00，提醒 小明：喝水"
    assert reminders[0]["target_user_id"] == "10002"
    assert format_due_reminder_message(reminders[0]) == "[CQ:at,qq=10002] 提醒：喝水"


def test_created_reminder_feedback_escapes_cq_without_mentioning_target(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(reminder_service, "DB_PATH", tmp_path / "reminders.db")
    monkeypatch.setattr(reminder_service, "_db_ready", False)

    async def run() -> dict[str, object]:
        return await create_reminder(
            ReminderScope(user_id="10001", target_type="group", group_id="20001"),
            "2099-01-01 09:00 提醒小明集合",
            target=ReminderTarget(user_id="10002", display_name="[CQ:at,qq=10002]小明"),
            content_override="[CQ:at,qq=all]集合",
        )

    created = asyncio.run(run())

    assert created["message"] == (
        "已创建提醒 #1：2099-01-01 09:00，提醒 &#91;CQ:at,qq=10002&#93;小明："
        "&#91;CQ:at,qq=all&#93;集合"
    )
