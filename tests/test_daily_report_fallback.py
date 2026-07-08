from __future__ import annotations

from datetime import date

from plugins.daily_report_fallback import fallback_chunk_summary, fallback_final_report


def test_fallback_chunk_summary_keeps_excerpt_after_timeout() -> None:
    summary = fallback_chunk_summary(
        "小拉：入口在哪里\n匣子：补给要更新\n草草：注意陷阱",
        1,
        3,
        TimeoutError("slow summary api"),
    )

    assert "分块 1/3 AI 摘要失败" in summary
    assert "TimeoutError" in summary
    assert "小拉：入口在哪里" in summary


def test_fallback_final_report_has_clear_warning_and_sections() -> None:
    report = fallback_final_report(
        group_id="1072225932",
        target_date=date(2026, 7, 7),
        stats_text="消息数：1157",
        chunk_summaries=[
            "- 入口路线讨论比较多",
            "- 补给和陷阱需要跟进",
            "- 图片素材已识别",
        ],
        error=TimeoutError("final synthesis timeout"),
    )

    assert report.startswith("# 群聊日报")
    assert "## 生成提示" in report
    assert "兜底版日报" in report
    assert "TimeoutError" in report
    assert "## 今日重点" in report
    assert "## 分话题总结" in report
