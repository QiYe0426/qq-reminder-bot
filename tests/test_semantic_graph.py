from __future__ import annotations

import asyncio

from PIL import Image

from plugins import access_control, agent_tool_access, message_archive, semantic_graph
from plugins.access_control import FEATURE_COLLECTOR, set_group_feature
from plugins.agent_tools import run_registered_agent_tool
from plugins.agent_tools import confirmation
from plugins.agent_tools.confirmation import confirm_pending_confirmation
from plugins.message_archive import insert_collected_message
from plugins.semantic_graph import build_semantic_graph_result, latest_semantic_graph, semantic_graph_summary
from plugins.semantic_graph_visual import render_semantic_graph_to_file


def reset_paths(tmp_path, monkeypatch) -> None:
    archive_db = tmp_path / "message_archive.db"
    graph_db = tmp_path / "semantic_graph.db"
    access_db = tmp_path / "bot_settings.db"
    monkeypatch.setattr(message_archive, "DB_PATH", archive_db)
    monkeypatch.setattr(semantic_graph, "ARCHIVE_DB_PATH", archive_db)
    monkeypatch.setattr(semantic_graph, "DB_PATH", graph_db)
    monkeypatch.setattr(access_control, "DB_PATH", access_db)
    monkeypatch.setattr(access_control, "_db_ready", False)
    monkeypatch.setattr(agent_tool_access, "ACCESS_DB_PATH", access_db)
    monkeypatch.setattr(agent_tool_access, "_db_ready", False)


async def seed_messages(group_id: str) -> None:
    rows = [
        ("m1", "10001", "小拉", "alpha beta treasure route"),
        ("m2", "10002", "匣子", "alpha route map image"),
        ("m3", "10001", "小拉", "beta treasure route"),
        ("m4", "10003", "草草", "team alpha supply route"),
    ]
    for index, (message_id, user_id, sender, text) in enumerate(rows, start=1):
        await insert_collected_message(
            message_id=message_id,
            self_id="999",
            group_id=group_id,
            user_id=user_id,
            sender_name_value=sender,
            message_type="group",
            sub_type="normal",
            segment_types=["text"],
            plain_text=text,
            raw_message=[{"type": "text", "data": {"text": text}}],
            event_json={},
            created_at=f"2026-07-08 10:0{index}:00",
        )


def test_semantic_graph_build_save_and_load(tmp_path, monkeypatch) -> None:
    reset_paths(tmp_path, monkeypatch)

    async def run() -> tuple[dict[str, object], dict[str, object] | None, str]:
        await seed_messages("1001")
        graph = await build_semantic_graph_result(group_id="1001", limit=20)
        loaded = await latest_semantic_graph("1001")
        summary = semantic_graph_summary(graph)
        return graph, loaded, summary

    graph, loaded, summary = asyncio.run(run())

    assert graph["ok"] is True
    assert graph["message_count"] == 4
    assert graph["node_count"] >= 4
    assert graph["edge_count"] >= 3
    assert loaded is not None
    assert loaded["graph_id"] == graph["graph_id"]
    assert "alpha" in summary


def test_semantic_graph_visual_renders_png(tmp_path, monkeypatch) -> None:
    reset_paths(tmp_path, monkeypatch)
    monkeypatch.setattr("plugins.semantic_graph_visual.GRAPH_DIR", tmp_path / "graphs")

    async def run() -> dict[str, object]:
        await seed_messages("1002")
        return await build_semantic_graph_result(group_id="1002", limit=20)

    graph = asyncio.run(run())
    filename, image_path = render_semantic_graph_to_file(graph)

    assert filename.endswith(".png")
    assert image_path.is_file()
    assert image_path.stat().st_size > 1000
    with Image.open(image_path) as image:
        w, h = image.size
        assert w >= 600 and h >= 600, f"Expected at least 600x600, got {w}x{h}"


def test_semantic_graph_agent_tools(tmp_path, monkeypatch) -> None:
    reset_paths(tmp_path, monkeypatch)
    monkeypatch.setattr("plugins.semantic_graph_visual.GRAPH_DIR", tmp_path / "graphs")
    monkeypatch.setattr(confirmation, "DB_PATH", tmp_path / "agent_tool_confirmations.db")

    async def allow_target_group(user_id: str, group_id: str, context: dict[str, object]) -> bool:
        return True

    monkeypatch.setattr(agent_tool_access, "target_group_admin_authorized", allow_target_group)

    async def run() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        await seed_messages("1003")
        await set_group_feature("1003", FEATURE_COLLECTOR, True)
        context = {"_target_type": "private", "_user_id": "1261957634", "_is_admin": True}
        build_args = {"group_id": "1003", "limit": 20}
        build_pending = await run_registered_agent_tool(
            "build_semantic_graph",
            build_args,
            context,
        )
        build_approval = await confirm_pending_confirmation(
            build_pending["data"]["confirmation_code"],
            user_id="1261957634",
            target_type="private",
            target_id="",
        )
        built = await run_registered_agent_tool(
            "build_semantic_graph",
            build_args,
            {**context, "_tool_confirmation_token": build_approval.token},
        )
        read = await run_registered_agent_tool(
            "get_semantic_graph",
            {"group_id": "1003"},
            context,
        )
        render_args = {"group_id": "1003", "send_image": False}
        render_pending = await run_registered_agent_tool(
            "render_semantic_graph",
            render_args,
            context,
        )
        render_approval = await confirm_pending_confirmation(
            render_pending["data"]["confirmation_code"],
            user_id="1261957634",
            target_type="private",
            target_id="",
        )
        rendered = await run_registered_agent_tool(
            "render_semantic_graph",
            render_args,
            {**context, "_tool_confirmation_token": render_approval.token},
        )
        return built, read, rendered

    built, read, rendered = asyncio.run(run())

    assert built["ok"] is True
    assert read["ok"] is True
    assert rendered["ok"] is True
    assert rendered["data"]["sent"] is False
    assert str(rendered["data"]["image_filename"]).endswith(".png")


def test_semantic_graph_id_must_belong_to_effective_group(tmp_path, monkeypatch) -> None:
    reset_paths(tmp_path, monkeypatch)

    async def allow_target_group(user_id: str, group_id: str, context: dict[str, object]) -> bool:
        return True

    monkeypatch.setattr(agent_tool_access, "target_group_admin_authorized", allow_target_group)

    async def run() -> dict[str, object]:
        await seed_messages("2001")
        await seed_messages("2002")
        await set_group_feature("2001", FEATURE_COLLECTOR, True)
        await set_group_feature("2002", FEATURE_COLLECTOR, True)
        other_group_graph = await build_semantic_graph_result(group_id="2002", limit=20)
        return await run_registered_agent_tool(
            "get_semantic_graph",
            {"group_id": "2001", "graph_id": other_group_graph["graph_id"], "auto_build": False},
            {"_target_type": "private", "_user_id": "42", "_is_admin": True},
        )

    result = asyncio.run(run())

    assert result["ok"] is False
    assert result["error"] == "group_permission_denied"
