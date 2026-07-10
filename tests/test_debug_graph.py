"""Debug: dump the actual graph data."""
import asyncio, json
from plugins.semantic_graph import build_semantic_graph_result


def test_dump_graph(tmp_path, monkeypatch) -> None:
    from tests.test_semantic_graph import reset_paths, seed_messages
    reset_paths(tmp_path, monkeypatch)

    async def run():
        await seed_messages("debug1")
        graph = await build_semantic_graph_result(group_id="debug1", limit=20)
        print(f"\n=== GRAPH DATA ===")
        print(f"Nodes ({graph['node_count']}):")
        for n in graph["nodes"]:
            print(f"  [{n['kind']:7s}] {n['label']!r:20s}  weight={n['weight']}")
        print(f"Edges ({graph['edge_count']}):")
        for e in graph["edges"]:
            print(f"  {e['source']!r:20s} --{e['relation']}→ {e['target']!r:20s}  weight={e['weight']}")
        print("=================")

    asyncio.run(run())
