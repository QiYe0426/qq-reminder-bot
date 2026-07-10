from datetime import datetime, timedelta

from plugins.reminder_service import ReminderTarget
from plugins.reminder_target_service import (
    SOURCE_MENTION,
    AuthorizedReminderTarget,
    GroupMember,
    authorize_confirmed_candidate,
    authorized_target_is_valid,
    find_target_in_content,
    renew_authorized_target,
    resolve_mentioned_targets,
    resolve_verified_target_in_content,
    strip_target_action_prefix,
    strip_target_pronoun,
    verified_group_members,
)


def test_strip_target_action_prefix() -> None:
    assert strip_target_action_prefix("提醒小明喝水") == ("小明喝水", True)
    assert strip_target_action_prefix("喝水") == ("喝水", False)


def test_find_target_in_content_exact_member_prefix() -> None:
    members = [
        GroupMember(user_id="10001", display_name="小明", aliases=("小明", "10001")),
        GroupMember(user_id="10002", display_name="小红", aliases=("小红", "10002")),
    ]

    match = find_target_in_content("提醒小明喝水", members)

    assert match is not None
    assert match.needs_confirmation is False
    assert match.target.user_id == "10001"
    assert match.target.display_name == "小明"
    assert match.content == "喝水"


def test_find_target_in_content_fuzzy_member_needs_confirmation() -> None:
    members = [
        GroupMember(user_id="10001", display_name="小明同学", aliases=("小明同学", "10001")),
    ]

    match = find_target_in_content("提醒小明喝水", members)

    assert match is not None
    assert match.needs_confirmation is True
    assert match.target.user_id == "10001"
    assert match.content == "喝水"


def test_find_target_in_content_fuzzy_without_action_when_allowed() -> None:
    members = [
        GroupMember(user_id="10001", display_name="小拉草草小匣", aliases=("小拉草草小匣", "10001")),
    ]

    match = find_target_in_content(
        "小拉现在抱抱小匣草草小匣",
        members,
        allow_fuzzy_without_action=True,
    )

    assert match is not None
    assert match.needs_confirmation is True
    assert match.target.user_id == "10001"
    assert match.content == "现在抱抱小匣草草小匣"


def test_find_target_in_content_self_reminder_returns_none() -> None:
    members = [
        GroupMember(user_id="10001", display_name="小明", aliases=("小明", "10001")),
    ]

    assert find_target_in_content("提醒我", members) is None


def test_strip_target_pronoun_for_recent_target() -> None:
    assert strip_target_pronoun("提醒她喝水") == ("喝水", True)
    assert strip_target_pronoun("叫 ta 一下吃药") == ("吃药", True)
    assert strip_target_pronoun("提醒那个人去睡觉") == ("睡觉", True)
    assert strip_target_pronoun("提醒小明喝水") == ("小明喝水", False)


def member_directory():
    return verified_group_members(
        "20001",
        [
            {"user_id": "10001", "card": "小明", "nickname": "明明"},
            {"user_id": "10002", "card": "小红", "nickname": "红红"},
        ],
    )


def test_mention_authorization_requires_verified_group_member() -> None:
    allowed = resolve_mentioned_targets(
        ["10002"],
        member_directory(),
        creator_user_id="10001",
        content="喝水",
    )
    denied = resolve_mentioned_targets(
        ["99999"],
        member_directory(),
        creator_user_id="10001",
        content="喝水",
    )

    assert allowed.authorized_target is not None
    assert allowed.authorized_target.source == SOURCE_MENTION
    assert allowed.authorized_target.target.user_id == "10002"
    assert denied.authorized_target is None
    assert "不是当前群" in denied.error


def test_multiple_mentions_are_ambiguous_and_not_authorized() -> None:
    resolution = resolve_mentioned_targets(
        ["10001", "10002"],
        member_directory(),
        creator_user_id="10001",
        content="喝水",
    )

    assert resolution.authorized_target is None
    assert len(resolution.candidates) == 2
    assert "一次只能提醒一个人" in resolution.error


def test_exact_member_match_is_authorized_and_fuzzy_match_requires_confirmation() -> None:
    exact = resolve_verified_target_in_content(
        "提醒小红喝水",
        member_directory(),
        creator_user_id="10001",
    )
    fuzzy = resolve_verified_target_in_content(
        "提醒红红同学喝水",
        verified_group_members(
            "20001",
            [{"user_id": "10002", "card": "红红同学本人", "nickname": "小红"}],
        ),
        creator_user_id="10001",
    )

    assert exact is not None and exact.authorized_target is not None
    assert exact.authorized_target.target.user_id == "10002"
    assert fuzzy is not None and fuzzy.candidate is not None
    assert fuzzy.authorized_target is None


def test_confirmed_candidate_is_bound_to_group_creator_and_target() -> None:
    now = datetime(2026, 7, 10, 12, 0)
    resolution = resolve_verified_target_in_content(
        "提醒红红喝水",
        verified_group_members(
            "20001",
            [{"user_id": "10002", "card": "红红同学", "nickname": "小红"}],
        ),
        creator_user_id="10001",
        now=now,
    )
    assert resolution is not None and resolution.candidate is not None

    allowed = authorize_confirmed_candidate(
        resolution.candidate,
        group_id="20001",
        creator_user_id="10001",
        target_user_id="10002",
        now=now,
    )
    wrong_group = authorize_confirmed_candidate(
        resolution.candidate,
        group_id="other",
        creator_user_id="10001",
        target_user_id="10002",
        now=now,
    )
    wrong_creator = authorize_confirmed_candidate(
        resolution.candidate,
        group_id="20001",
        creator_user_id="other",
        target_user_id="10002",
        now=now,
    )
    expired = authorize_confirmed_candidate(
        resolution.candidate,
        group_id="20001",
        creator_user_id="10001",
        target_user_id="10002",
        now=now + timedelta(minutes=6),
    )

    assert allowed is not None
    assert allowed.source == "confirmed_candidate"
    assert wrong_group is None
    assert wrong_creator is None
    assert expired is None


def test_authorized_recent_target_keeps_source_and_checks_binding_and_ttl() -> None:
    now = datetime(2026, 7, 10, 12, 0)
    authorized = AuthorizedReminderTarget(
        group_id="20001",
        creator_user_id="10001",
        target=ReminderTarget("10002", "小红"),
        source=SOURCE_MENTION,
        expires_at=now + timedelta(minutes=1),
    )
    recent = renew_authorized_target(authorized, ttl=timedelta(hours=2), now=now)

    assert recent.source == SOURCE_MENTION
    assert authorized_target_is_valid(recent, group_id="20001", creator_user_id="10001", now=now)
    assert not authorized_target_is_valid(recent, group_id="other", creator_user_id="10001", now=now)
    assert not authorized_target_is_valid(recent, group_id="20001", creator_user_id="other", now=now)
    assert not authorized_target_is_valid(
        recent,
        group_id="20001",
        creator_user_id="10001",
        now=now + timedelta(hours=3),
    )
