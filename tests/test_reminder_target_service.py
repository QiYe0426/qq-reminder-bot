from plugins.reminder_target_service import GroupMember, find_target_in_content, strip_target_action_prefix


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


def test_find_target_in_content_self_reminder_returns_none() -> None:
    members = [
        GroupMember(user_id="10001", display_name="小明", aliases=("小明", "10001")),
    ]

    assert find_target_in_content("提醒我", members) is None
