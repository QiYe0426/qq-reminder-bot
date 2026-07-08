from __future__ import annotations


REMINDER_CONFIRMATION_TITLE = "【提醒确认】"
REMINDER_TIME_TITLE = "【提醒时间】"


def reminder_confirmation_prompt(
    target_name: str,
    *,
    prefix: str = "",
    allow_time_reply: bool = False,
) -> str:
    lines = [REMINDER_CONFIRMATION_TITLE]
    if prefix:
        lines.append(prefix)
    lines.append(f"你想提醒的是 {target_name} 对吗？")
    if allow_time_reply:
        lines.append("回复“对”或“不对”，也可以直接 @某人；时间也可以直接说“一分钟后”。")
    else:
        lines.append("回复“对”或“不对”，也可以直接 @某人。")
    lines.append("这个确认在 1 分钟内有效。")
    return "\n".join(lines)


def reminder_time_wait_prompt(target_name: str, *, content: str = "") -> str:
    lines = [REMINDER_TIME_TITLE]
    if content:
        lines.append(f"我知道要提醒 {target_name}：{content}")
    else:
        lines.append(f"对象确认了：{target_name}")
    lines.extend(
        [
            "再告诉我什么时候提醒，比如“一分钟后”或“明早9点”。",
            "这个等待在 1 分钟内有效。",
        ]
    )
    return "\n".join(lines)
