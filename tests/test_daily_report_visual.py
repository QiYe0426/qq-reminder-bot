from __future__ import annotations

from datetime import date

from PIL import Image
from pypdf import PdfReader

from plugins.daily_report_visual import (
    build_visual_data,
    render_daily_report_image,
    write_daily_report_pdf_from_image,
)


def test_render_daily_report_image_and_pdf(tmp_path) -> None:
    markdown = """# 群聊日报
## 今日重点
- 新宝物线索出现，大家在讨论入口和安全区。
- 队伍补给已经更新，需要后续确认分配。
- 有几张图片素材完成识别，可以回看。

## 分话题总结
- 遗迹入口讨论：群友集中讨论入口坐标和路线。
- 宝物识别：图片素材里出现了可疑道具。
- 队伍配置：有人建议调整职业搭配。
- 补给与陷阱：提醒大家检查补给。

## 统计信息
消息数：12
活跃发言人：3
初步话题线索：入口:4, 宝物:3, 补给:2, 陷阱:2
"""
    rows = [
        {
            "id": 1,
            "sender_name": "小拉",
            "user_id": "10001",
            "sub_type": "normal",
            "created_at": "2026-07-08 09:10:00",
            "segment_types": '["text"]',
            "plain_text": "入口坐标是不是在这里",
        },
        {
            "id": 2,
            "sender_name": "匣子",
            "user_id": "10002",
            "sub_type": "normal",
            "created_at": "2026-07-08 09:18:00",
            "segment_types": '["image"]',
            "plain_text": "我发了宝物图片",
        },
        {
            "id": 3,
            "sender_name": "草草",
            "user_id": "10003",
            "sub_type": "normal",
            "created_at": "2026-07-08 21:30:00",
            "segment_types": '["text"]',
            "plain_text": "补给和陷阱都要看一下",
        },
    ]
    insights = {2: [{"insight_type": "image", "status": "done", "content": "宝物图片"}]}
    data = build_visual_data(
        group_id="722290838",
        target_date=date(2026, 7, 8),
        group_name="冒险交流群",
        markdown_content=markdown,
        rows=rows,
        insights_by_message_id=insights,
    )
    assert data.highlights[0].startswith("新宝物线索出现")
    assert data.topics[0] == "遗迹入口讨论"
    assert data.keywords[:2] == ["入口", "宝物"]

    image_path = tmp_path / "daily-report.png"
    pdf_path = tmp_path / "daily-report.pdf"

    render_daily_report_image(data, image_path)
    write_daily_report_pdf_from_image(image_path, pdf_path)

    with Image.open(image_path) as image:
        assert image.size[0] == 1080
        assert image.size[1] > 2500

    reader = PdfReader(str(pdf_path))
    assert len(reader.pages) == 1
    assert pdf_path.stat().st_size > image_path.stat().st_size * 0.1
