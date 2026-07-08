from __future__ import annotations

from datetime import date


def _compact_line(text: str, limit: int = 180) -> str:
    text = " ".join(str(text or "").split()).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def fallback_chunk_summary(chunk_text: str, index: int, total: int, error: BaseException) -> str:
    excerpts: list[str] = []
    for line in chunk_text.splitlines():
        stripped = _compact_line(line.strip(), 180)
        if stripped and stripped not in excerpts:
            excerpts.append(stripped)
        if len(excerpts) >= 8:
            break
    excerpt_text = "\n".join(f"- {item}" for item in excerpts) or "- 该分块没有可摘录文本。"
    return "\n".join(
        [
            f"分块 {index}/{total} AI 摘要失败，已使用自动摘录兜底。",
            f"失败原因：{type(error).__name__}: {str(error)[:180]}",
            "自动摘录：",
            excerpt_text,
        ]
    )


def fallback_final_report(
    *,
    group_id: str,
    target_date: date,
    stats_text: str,
    chunk_summaries: list[str],
    error: BaseException,
) -> str:
    topic_lines: list[str] = []
    for summary in chunk_summaries:
        for raw_line in summary.splitlines():
            line = _compact_line(raw_line.strip().lstrip("-0123456789.、) "), 140)
            if not line or line.startswith("分块") or line.startswith("失败原因"):
                continue
            if line not in topic_lines:
                topic_lines.append(line)
            if len(topic_lines) >= 12:
                break
        if len(topic_lines) >= 12:
            break

    highlights = topic_lines[:5] or ["已采集到消息，但最终 AI 合成超时；请查看附录中的结构化时间线。"]
    topics = topic_lines[5:11] or topic_lines[:4] or ["暂无可用分块摘要。"]
    return "\n".join(
        [
            "# 群聊日报",
            "",
            "## 生成提示",
            "",
            "- 最终 AI 合成超时或失败，猎宝已用分块摘要自动生成兜底版日报。",
            f"- 失败原因：{type(error).__name__}: {str(error)[:180]}",
            "- 这份日报不会漏发，但表达质量可能低于正常 AI 合成版。",
            "",
            "## 今日重点",
            "",
            *(f"- {item}" for item in highlights),
            "",
            "## 分话题总结",
            "",
            *(f"- {item}" for item in topics),
            "",
            "## 重要链接和素材",
            "",
            "- 请查看 Markdown 附录中的结构化时间线和素材识别结果。",
            "",
            "## 待办与跟进",
            "",
            "- 兜底版未进行深度归纳，请管理员按需回看原文线索。",
            "",
            "## 风险/争议/低置信度",
            "",
            "- 本版由程序兜底生成，结论置信度低于正常 AI 合成版。",
            "",
            "## 值得回看的原文线索",
            "",
            "- 详见附录：结构化时间线。",
        ]
    )
