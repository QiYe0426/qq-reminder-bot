from plugins.reminder_prompt import reminder_confirmation_prompt, reminder_time_wait_prompt


def test_reminder_confirmation_prompt_has_clear_title() -> None:
    prompt = reminder_confirmation_prompt("小拉", prefix="我先确认一下。", allow_time_reply=True)

    assert prompt.splitlines()[0] == "【提醒确认】"
    assert "你想提醒的是 小拉 对吗？" in prompt
    assert "回复“对”或“不对”" in prompt
    assert "这个确认在 1 分钟内有效。" in prompt
    assert "一分钟后" in prompt


def test_reminder_time_wait_prompt_has_clear_title() -> None:
    prompt = reminder_time_wait_prompt("小拉", content="喝水")

    assert prompt.splitlines()[0] == "【提醒时间】"
    assert "我知道要提醒 小拉：喝水" in prompt
    assert "这个等待在 1 分钟内有效。" in prompt
