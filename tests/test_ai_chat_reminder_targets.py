import asyncio
from datetime import datetime, timedelta

import nonebot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.event import Sender


nonebot.init()

from plugins import ai_chat
from plugins.reminder_service import ReminderTarget
from plugins.reminder_target_service import (
    SOURCE_MENTION,
    AuthorizedReminderTarget,
)


class FakeBot:
    self_id = "999"

    def __init__(self, members: list[dict[str, object]] | None = None, *, fail: bool = False) -> None:
        self.members = members or []
        self.fail = fail

    async def get_group_member_list(self, group_id: int) -> list[dict[str, object]]:
        if self.fail:
            raise RuntimeError("member lookup failed")
        return self.members


def group_event(
    *,
    user_id: int = 10001,
    group_id: int = 20001,
    target_ids: tuple[int, ...] = (),
    text: str = "提醒",
) -> GroupMessageEvent:
    segments = [MessageSegment.at(target_id) for target_id in target_ids]
    segments.append(MessageSegment.text(text))
    message = Message(segments)
    return GroupMessageEvent(
        time=0,
        self_id=999,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        message_id=1,
        message=message,
        original_message=message,
        raw_message=text,
        font=0,
        sender=Sender(user_id=user_id, nickname="creator", card="creator", role="member"),
        to_me=True,
        group_id=group_id,
    )


def group_members() -> list[dict[str, object]]:
    return [
        {"user_id": 10001, "card": "creator", "nickname": "creator"},
        {"user_id": 10002, "card": "小红", "nickname": "red"},
        {"user_id": 10003, "card": "小明", "nickname": "ming"},
    ]


def test_multiple_mentions_never_select_the_first_target() -> None:
    event = group_event(target_ids=(10002, 10003))
    resolution = asyncio.run(
        ai_chat.direct_group_target_match(
            "2099-01-01 09:00 喝水",
            event,
            FakeBot(group_members()),
        )
    )

    assert resolution is not None
    assert resolution.authorized_target is None
    assert len(resolution.candidates) == 2
    assert "一次只能提醒一个人" in resolution.error


def test_member_lookup_failure_rejects_explicit_target() -> None:
    event = group_event(target_ids=(10002,))
    resolution = asyncio.run(
        ai_chat.direct_group_target_match(
            "2099-01-01 09:00 喝水",
            event,
            FakeBot(fail=True),
        )
    )

    assert resolution is not None
    assert resolution.authorized_target is None
    assert "无法验证群成员" in resolution.error


def test_member_lookup_failure_does_not_block_plain_self_reminder() -> None:
    resolution = asyncio.run(
        ai_chat.direct_group_target_match(
            "2099-01-01 09:00 喝水",
            group_event(),
            FakeBot(fail=True),
        )
    )

    assert resolution is None


def test_verified_mention_creates_authorized_target() -> None:
    event = group_event(target_ids=(10002,))
    resolution = asyncio.run(
        ai_chat.direct_group_target_match(
            "2099-01-01 09:00 喝水",
            event,
            FakeBot(group_members()),
        )
    )

    assert resolution is not None
    assert resolution.authorized_target is not None
    assert resolution.authorized_target.target.user_id == "10002"
    assert resolution.authorized_target.source == SOURCE_MENTION


def test_recent_target_checks_binding_and_keeps_authorization_source() -> None:
    ai_chat.recent_reminder_targets.clear()
    event = group_event()
    authorized = AuthorizedReminderTarget(
        group_id="20001",
        creator_user_id="10001",
        target=ReminderTarget("10002", "小红"),
        source=SOURCE_MENTION,
        expires_at=datetime.now() + timedelta(minutes=1),
    )

    ai_chat.remember_recent_reminder_target(event, authorized)
    recent = ai_chat.recent_reminder_target(event)

    assert recent is not None
    assert recent.source == SOURCE_MENTION
    assert recent.target.user_id == "10002"

    reused = asyncio.run(
        ai_chat.direct_group_target_match(
            "2099-01-01 09:00 提醒她喝水",
            event,
            FakeBot(fail=True),
        )
    )
    assert reused is not None
    assert reused.authorized_target is not None
    assert reused.authorized_target.source == SOURCE_MENTION

    wrong_creator_event = group_event(user_id=10004)
    ai_chat.recent_reminder_targets[("20001", "10004")] = ai_chat.RecentReminderTarget(
        group_id="20001",
        creator_user_id="10004",
        authorized_target=recent,
    )
    assert ai_chat.recent_reminder_target(wrong_creator_event) is None


def test_pending_confirmation_rejects_tampered_creator_binding() -> None:
    ai_chat.pending_reminder_confirmations.clear()
    event = group_event()
    directory = ai_chat.verified_group_members(
        "20001",
        [{"user_id": 10002, "card": "红红同学本人", "nickname": "小红"}],
    )
    resolution = ai_chat.resolve_verified_target_in_content(
        "提醒红红同学喝水",
        directory,
        creator_user_id="10001",
    )
    assert resolution is not None and resolution.candidate is not None

    wrong_creator_event = group_event(user_id=10004)
    ai_chat.pending_reminder_confirmations[("20001", "10004")] = ai_chat.PendingReminderConfirmation(
        group_id="20001",
        creator_user_id="10004",
        raw_text="2099-01-01 09:00 提醒红红同学喝水",
        content="喝水",
        candidate=resolution.candidate,
        expires_at=datetime.now() + timedelta(minutes=1),
    )

    assert ai_chat.pending_reminder_confirmation(wrong_creator_event) is None
    assert ("20001", "10004") not in ai_chat.pending_reminder_confirmations


def test_final_creation_requires_authorized_target(monkeypatch) -> None:
    event = group_event()
    calls: list[ReminderTarget] = []

    async def fake_create_reminder(scope, raw_text, *, target=None, content_override=None):
        calls.append(target)
        return {"ok": True, "message": "created"}

    monkeypatch.setattr(ai_chat, "create_reminder", fake_create_reminder)
    raw_result = asyncio.run(
        ai_chat.create_targeted_reminder_reply(
            event,
            raw_text="2099-01-01 09:00 喝水",
            authorized_target=ReminderTarget("10002", "小红"),
            content="喝水",
        )
    )
    authorized_result = asyncio.run(
        ai_chat.create_targeted_reminder_reply(
            event,
            raw_text="2099-01-01 09:00 喝水",
            authorized_target=AuthorizedReminderTarget(
                group_id="20001",
                creator_user_id="10001",
                target=ReminderTarget("10002", "小红"),
                source=SOURCE_MENTION,
                expires_at=datetime.now() + timedelta(minutes=1),
            ),
            content="喝水",
        )
    )

    assert "授权无效" in raw_result
    assert authorized_result == "created"
    assert [target.user_id for target in calls] == ["10002"]
