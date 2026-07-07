from __future__ import annotations

from plugins.access_control import FEATURE_AI_CHAT
from plugins.knowledge_service import search_knowledge_result

from .registry import AgentTool, AgentToolContext, AgentToolResult


def _tool_definition(
    *,
    name: str,
    description: str,
    properties: dict[str, object],
    required: list[str] | None = None,
) -> dict[str, object]:
    parameters: dict[str, object] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        parameters["required"] = required
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


async def search_sts2_knowledge_tool(args: dict[str, object], context: AgentToolContext) -> AgentToolResult:
    query = str(args.get("query") or "").strip()
    if not query:
        return {
            "ok": False,
            "error": "empty_query",
            "message": "缺少要查询的 STS2 关键词。",
        }

    raw_limit = args.get("limit")
    limit = int(raw_limit) if isinstance(raw_limit, (int, float, str)) and str(raw_limit).isdigit() else 5
    return await search_knowledge_result(query, category_prefix="STS2", limit=limit)


KNOWLEDGE_TOOLS = [
    AgentTool(
        name="search_sts2_knowledge",
        category="knowledge",
        requires_feature=FEATURE_AI_CHAT,
        definition=_tool_definition(
            name="search_sts2_knowledge",
            description=(
                "Search the local Slay the Spire 2 / STS2 knowledge base. "
                "Use this for questions about STS2 cards, relics, characters, enemies, bosses, events, keywords, mechanics, or guides."
            ),
            properties={
                "query": {
                    "type": "string",
                    "description": "Short STS2 lookup query, such as a card/relic name, mechanic, enemy, or guide topic.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum result count. Defaults to 5 and is capped internally.",
                },
            },
            required=["query"],
        ),
        handler=search_sts2_knowledge_tool,
    ),
]
