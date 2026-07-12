from __future__ import annotations

import copy
from dataclasses import dataclass

import pytest

from plugins.agent_tools.contracts import ToolResult, tool_success
from plugins.agent_tools.output_reducer_capability_review import (
    OUTPUT_REDUCER_CAPABILITY_PROPOSALS,
)
from plugins.agent_tools.output_reducer_inventory import OUTPUT_REDUCER_INVENTORY


Path = tuple[str, ...]


@dataclass(frozen=True)
class HandlerShapedFixture:
    tool_name: str
    branch: str
    result: ToolResult


def _nodes(*, optional_metadata: bool = True) -> list[dict[str, object]]:
    return [
        {
            "label": "Alice",
            "kind": "person",
            "weight": 8.0,
            "metadata": {"message_count": 8} if optional_metadata else {},
        },
        {
            "label": "Output Governance",
            "kind": "topic",
            "weight": 5.0,
            "metadata": {"mention_count": 5} if optional_metadata else {},
        },
    ]


def _edges(*, optional_metadata: bool = True) -> list[dict[str, object]]:
    return [
        {
            "source": "Alice",
            "source_kind": "person",
            "target": "Output Governance",
            "target_kind": "topic",
            "relation": "提到",
            "weight": 3.0,
            "metadata": {"turn_count": 3} if optional_metadata else {},
        }
    ]


def _build_graph_data(*, summary: str, optional_metadata: bool = True) -> dict[str, object]:
    nodes = _nodes(optional_metadata=optional_metadata)
    edges = _edges(optional_metadata=optional_metadata)
    return {
        "graph_id": "group-1:recent:240",
        "group_id": "group-1",
        "scope": "recent",
        "source_key": "recent:240",
        "title": "群聊语义图",
        "keyword": "",
        "message_count": 24,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": nodes,
        "edges": edges,
        "generated_at": "2026-07-12T10:00:00+08:00",
        "summary": summary,
    }


def _loaded_graph_data(*, summary: str, optional_metadata: bool = True) -> dict[str, object]:
    data = _build_graph_data(summary=summary, optional_metadata=optional_metadata)
    data["updated_at"] = data.pop("generated_at")
    return data


FIXTURES = (
    HandlerShapedFixture(
        "generate_daily_report",
        "existing_report_files",
        tool_success(
            {
                "group_id": "group-1",
                "date": "2026-07-11",
                "filename": "daily-report.md",
                "image_filename": "daily-report.png",
                "pdf_filename": "daily-report.pdf",
                "report": "已有日报正文",
                "truncated": False,
                "preview_chars": 2400,
                "reused_existing": True,
            },
            "已读取 2026-07-11 的已生成日报。",
        ),
    ),
    HandlerShapedFixture(
        "generate_daily_report",
        "new_report_missing_optional_files",
        tool_success(
            {
                "group_id": "group-1",
                "date": "2026-07-11",
                "filename": "daily-report.md",
                "report": "新生成日报正文",
                "truncated": False,
                "preview_chars": 1800,
                "reused_existing": False,
            },
            "已生成 2026-07-11 的日报。",
        ),
    ),
    HandlerShapedFixture(
        "generate_daily_report",
        "handler_truncated_long_report",
        tool_success(
            {
                "group_id": "group-1",
                "date": "2026-07-11",
                "filename": "daily-report.md",
                "report": "日报正文" * 1498 + "\n...",
                "truncated": True,
                "preview_chars": 9000,
                "reused_existing": False,
            },
            "已生成 2026-07-11 的日报。",
        ),
    ),
    HandlerShapedFixture("build_semantic_graph", "normal", tool_success(_build_graph_data(summary="语义图摘要"), "已生成语义图：2 个节点，1 条关系。")),
    HandlerShapedFixture("build_semantic_graph", "long_summary", tool_success(_build_graph_data(summary="长摘要" * 2000), "已生成语义图：2 个节点，1 条关系。")),
    HandlerShapedFixture("build_semantic_graph", "optional_metadata_absent", tool_success(_build_graph_data(summary="空 metadata", optional_metadata=False), "已生成语义图：2 个节点，1 条关系。")),
    HandlerShapedFixture("get_semantic_graph", "normal", tool_success(_loaded_graph_data(summary="读取摘要"), "已读取语义图。")),
    HandlerShapedFixture("get_semantic_graph", "long_summary", tool_success(_loaded_graph_data(summary="长摘要" * 2000), "已读取语义图。")),
    HandlerShapedFixture("get_semantic_graph", "optional_metadata_absent", tool_success(_loaded_graph_data(summary="空 metadata", optional_metadata=False), "已读取语义图。")),
    HandlerShapedFixture("render_semantic_graph", "source_summary_sent", tool_success({"graph_id": "group-1:recent:240", "group_id": "group-1", "image_filename": "semantic-graph.png", "image_path": "data/semantic_graph/semantic-graph.png", "sent": True, "send_error": "", "summary": "持久化来源 summary"}, "语义图可视化已生成。 已发送图片。")),
    HandlerShapedFixture("render_semantic_graph", "regenerated_summary_not_sent", tool_success({"graph_id": "group-1:recent:240", "group_id": "group-1", "image_filename": "semantic-graph.png", "image_path": "data/semantic_graph/semantic-graph.png", "sent": False, "send_error": "", "summary": "由 graph 结构重新生成的 summary"}, "语义图可视化已生成。 图片未自动发送，可在服务器文件中查看。")),
    HandlerShapedFixture("render_semantic_graph", "send_failure", tool_success({"graph_id": "group-1:recent:240", "group_id": "group-1", "image_filename": "semantic-graph.png", "image_path": "data/semantic_graph/semantic-graph.png", "sent": False, "send_error": "RuntimeError('send failed')", "summary": "长摘要" * 2000}, "语义图可视化已生成。 图片未自动发送，可在服务器文件中查看。")),
)


def _data_paths(value: object, path: Path = ("data",)) -> set[Path]:
    paths: set[Path] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            item_path = (*path, str(key))
            paths.add(item_path)
            paths.update(_data_paths(item, item_path))
    elif isinstance(value, list):
        list_path = (*path, "[]")
        for item in value:
            paths.update(_data_paths(item, list_path))
    return paths


def _assert_fixture_matches_inventory(fixture: HandlerShapedFixture) -> None:
    item = OUTPUT_REDUCER_INVENTORY[fixture.tool_name]
    assert fixture.result["ok"] is True
    assert set(fixture.result) == {"ok", "data", "message"}
    actual = _data_paths(fixture.result["data"])
    unknown = actual - set(item.output_structure)
    assert not unknown, f"{fixture.tool_name}/{fixture.branch} unknown paths: {unknown}"
    for candidate in item.candidate_paths:
        assert candidate in actual or candidate in item.optional_paths


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda item: f"{item.tool_name}-{item.branch}")
def test_handler_shaped_fixture_paths_are_inventory_aligned(fixture: HandlerShapedFixture) -> None:
    _assert_fixture_matches_inventory(fixture)


def test_unknown_data_field_causes_drift_guard_failure() -> None:
    fixture = copy.deepcopy(FIXTURES[3])
    fixture.result["data"]["new_unreviewed_field"] = "must fail closed"
    with pytest.raises(AssertionError, match="unknown paths"):
        _assert_fixture_matches_inventory(fixture)


def test_optional_paths_are_explicit_and_branch_specific() -> None:
    daily = OUTPUT_REDUCER_INVENTORY["generate_daily_report"]
    assert set(daily.optional_paths) == {
        ("data", "image_filename"),
        ("data", "pdf_filename"),
    }
    new_report = next(item for item in FIXTURES if item.branch == "new_report_missing_optional_files")
    actual = _data_paths(new_report.result["data"])
    assert not (set(daily.optional_paths) & actual)


def test_candidates_and_blocked_paths_remain_disjoint() -> None:
    for item in OUTPUT_REDUCER_INVENTORY.values():
        assert not (set(item.candidate_paths) & set(item.blocked_paths))


@pytest.mark.parametrize("tool_name", ["get_group_profile", "get_member_profile"])
def test_review_tools_still_have_no_capability_proposal(tool_name: str) -> None:
    assert OUTPUT_REDUCER_INVENTORY[tool_name].review_status == "review"
    assert tool_name not in OUTPUT_REDUCER_CAPABILITY_PROPOSALS
