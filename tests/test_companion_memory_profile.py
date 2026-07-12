from __future__ import annotations

import sqlite3

import aiosqlite
import nonebot


try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from plugins.companion_memory import profile_to_text  # noqa: E402


def _profile_row(*, longterm_profile: str) -> sqlite3.Row:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT
                '正在学习测试' AS current_activity,
                '直接友好' AS personality_notes,
                '偏好简洁提醒' AS emotional_preferences,
                '["测试", "机器人"]' AS topics,
                '原有画像摘要' AS summary,
                ? AS longterm_profile,
                '2026-07-13 01:00:00' AS updated_at,
                0.75 AS confidence
            """,
            (longterm_profile,),
        ).fetchone()
        assert row is not None
        return row
    finally:
        connection.close()


def test_profile_to_text_accepts_sqlite_row_with_longterm_profile() -> None:
    profile = _profile_row(longterm_profile="长期喜欢研究自动化")

    assert isinstance(profile, sqlite3.Row)
    assert isinstance(profile, aiosqlite.Row)
    assert profile_to_text(profile) == "\n".join(
        (
            "近期在做：正在学习测试",
            "互动风格：直接友好",
            "陪伴偏好：偏好简洁提醒",
            "常聊主题：测试、机器人",
            "画像摘要：原有画像摘要",
            "长期画像：长期喜欢研究自动化",
            "更新时间：2026-07-13 01:00:00",
            "置信度：0.75",
        )
    )


def test_profile_to_text_omits_empty_longterm_profile() -> None:
    profile = _profile_row(longterm_profile="")

    rendered = profile_to_text(profile)

    assert "长期画像：" not in rendered
    assert rendered == "\n".join(
        (
            "近期在做：正在学习测试",
            "互动风格：直接友好",
            "陪伴偏好：偏好简洁提醒",
            "常聊主题：测试、机器人",
            "画像摘要：原有画像摘要",
            "更新时间：2026-07-13 01:00:00",
            "置信度：0.75",
        )
    )


def test_profile_to_text_returns_empty_for_none() -> None:
    assert profile_to_text(None) == ""
