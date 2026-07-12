from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from collections.abc import Mapping

from .output_reducer_capability import OutputPath


_RISK_LEVELS = {"low", "medium", "high", "critical"}
_REVIEW_STATUSES = {"allow", "review", "deny"}


@dataclass(frozen=True)
class OutputReducerInventoryItem:
    """Static audit record; it does not authorize or construct a reducer."""

    tool_name: str
    output_structure: tuple[OutputPath, ...]
    candidate_paths: tuple[OutputPath, ...]
    blocked_paths: tuple[OutputPath, ...]
    risk_level: str
    reason: str
    review_status: str
    optional_paths: tuple[OutputPath, ...] = ()

    def __post_init__(self) -> None:
        if not self.tool_name or not self.tool_name.strip():
            raise ValueError("Output reducer inventory requires a tool name.")
        if self.risk_level not in _RISK_LEVELS:
            raise ValueError("Invalid output reducer inventory risk level.")
        if self.review_status not in _REVIEW_STATUSES:
            raise ValueError("Invalid output reducer inventory review status.")
        if not self.reason or not self.reason.strip():
            raise ValueError("Output reducer inventory requires an audit reason.")
        for collection_name, paths in (
            ("output_structure", self.output_structure),
            ("candidate_paths", self.candidate_paths),
            ("blocked_paths", self.blocked_paths),
            ("optional_paths", self.optional_paths),
        ):
            if not isinstance(paths, tuple):
                raise TypeError(f"{collection_name} must be a tuple.")
            if len(set(paths)) != len(paths):
                raise ValueError(f"{collection_name} paths must be unique.")
            for path in paths:
                if not isinstance(path, tuple) or not path:
                    raise TypeError(f"{collection_name} paths must be non-empty tuples.")
                if any(not isinstance(part, str) or not part for part in path):
                    raise TypeError(f"{collection_name} path parts must be strings.")
                if path[0] != "data":
                    raise ValueError(f"{collection_name} paths must use data namespace.")
        if set(self.candidate_paths) & set(self.blocked_paths):
            raise ValueError("Candidate and blocked paths must not overlap.")
        if not set(self.candidate_paths).issubset(self.output_structure):
            raise ValueError("Candidate paths must exist in output_structure.")
        if not set(self.optional_paths).issubset(self.output_structure):
            raise ValueError("Optional paths must exist in output_structure.")
        if self.review_status == "deny" and self.candidate_paths:
            raise ValueError("Denied inventory items cannot expose candidate paths.")


def _item(
    tool_name: str,
    *,
    output: tuple[OutputPath, ...],
    candidates: tuple[OutputPath, ...] = (),
    blocked: tuple[OutputPath, ...] = (),
    risk: str,
    reason: str,
    status: str,
    optional: tuple[OutputPath, ...] = (),
) -> OutputReducerInventoryItem:
    return OutputReducerInventoryItem(
        tool_name=tool_name,
        output_structure=output,
        candidate_paths=candidates,
        blocked_paths=blocked,
        risk_level=risk,
        reason=reason,
        review_status=status,
        optional_paths=optional,
    )


# Explicit audit inventory. This is deliberately not derived from handlers,
# registered tools, sample results, metadata, or string scanning.
_ITEMS = (
    _item("get_group_status", output=(("data", "group_id"), ("data", "features"), ("data", "archive"), ("data", "group_profile"), ("data", "agent_tools"), ("data", "daily_report_runs")), blocked=(("data", "group_id"), ("data", "features"), ("data", "archive"), ("data", "group_profile"), ("data", "agent_tools"), ("data", "daily_report_runs")), risk="high", reason="Identifiers, switches, counters and run state are operational semantics.", status="deny"),
    _item("generate_daily_report", output=(("data", "group_id"), ("data", "date"), ("data", "filename"), ("data", "image_filename"), ("data", "pdf_filename"), ("data", "report"), ("data", "truncated"), ("data", "preview_chars"), ("data", "reused_existing")), candidates=(("data", "report"),), blocked=(("data", "group_id"), ("data", "date"), ("data", "filename"), ("data", "image_filename"), ("data", "pdf_filename"), ("data", "truncated"), ("data", "preview_chars"), ("data", "reused_existing")), optional=(("data", "image_filename"), ("data", "pdf_filename")), risk="high", reason="Report is handler-truncated display text; image/PDF filenames exist only when reusing an existing report. Identity, date, files, counts and branch state are protected.", status="allow"),
    _item("get_group_profile", output=(("data", "group_id"), ("data", "profile"), ("data", "profile", "summary")), candidates=(("data", "profile", "summary"),), blocked=(("data", "group_id"),), risk="medium", reason="Nested summary may be display text, but all other profile fields remain outside the allowlist pending manual schema review.", status="review"),
    _item("get_member_profile", output=(("data", "group_id"), ("data", "member_id"), ("data", "profile"), ("data", "profile", "summary")), candidates=(("data", "profile", "summary"),), blocked=(("data", "group_id"), ("data", "member_id")), risk="high", reason="Summary is reviewable; identity is blocked and all other profile fields remain outside the allowlist.", status="review"),
    _item("set_group_features", output=(("data", "group_id"), ("data", "enabled"), ("data", "disabled"), ("data", "features")), blocked=(("data", "group_id"), ("data", "enabled"), ("data", "disabled"), ("data", "features")), risk="critical", reason="The complete result describes authorization-relevant switch mutations.", status="deny"),
    _item("set_chime", output=(("data", "enabled"), ("data", "mode"), ("data", "group_id")), blocked=(("data", "enabled"), ("data", "mode"), ("data", "group_id")), risk="high", reason="All fields describe applied configuration or target identity.", status="deny"),
    _item("get_group_context", output=(("data", "group_id"), ("data", "context"), ("data", "updated_at")), blocked=(("data", "group_id"), ("data", "context"), ("data", "updated_at")), risk="high", reason="Context is source data rather than presentation-only generated text.", status="deny"),
    _item("search_sts2_knowledge", output=(("data", "query"), ("data", "results")), blocked=(("data", "query"), ("data", "results")), risk="high", reason="Knowledge excerpts and result structure must remain attributable and complete.", status="deny"),
    _item("create_reminder", output=(("data", "reminder_id"), ("data", "target_user_id"), ("data", "remind_at"), ("data", "content")), blocked=(("data", "reminder_id"), ("data", "target_user_id"), ("data", "remind_at"), ("data", "content")), risk="critical", reason="Reminder identity, ownership, schedule and content are execution semantics.", status="deny"),
    _item("list_reminders", output=(("data", "reminders"),), blocked=(("data", "reminders"),), risk="critical", reason="Reminder collection entries contain IDs, ownership, schedule and task content.", status="deny"),
    _item("cancel_reminder", output=(("data", "reminder_id"), ("data", "cancelled")), blocked=(("data", "reminder_id"), ("data", "cancelled")), risk="critical", reason="Cancellation identity and outcome must remain intact.", status="deny"),
    _item("build_semantic_graph", output=(("data", "graph_id"), ("data", "group_id"), ("data", "scope"), ("data", "source_key"), ("data", "title"), ("data", "keyword"), ("data", "message_count"), ("data", "node_count"), ("data", "edge_count"), ("data", "nodes"), ("data", "nodes", "[]", "label"), ("data", "nodes", "[]", "kind"), ("data", "nodes", "[]", "weight"), ("data", "nodes", "[]", "metadata"), ("data", "nodes", "[]", "metadata", "message_count"), ("data", "nodes", "[]", "metadata", "mention_count"), ("data", "edges"), ("data", "edges", "[]", "source"), ("data", "edges", "[]", "source_kind"), ("data", "edges", "[]", "target"), ("data", "edges", "[]", "target_kind"), ("data", "edges", "[]", "relation"), ("data", "edges", "[]", "weight"), ("data", "edges", "[]", "metadata"), ("data", "edges", "[]", "metadata", "turn_count"), ("data", "generated_at"), ("data", "summary")), candidates=(("data", "summary"),), blocked=(("data", "graph_id"), ("data", "group_id"), ("data", "scope"), ("data", "source_key"), ("data", "title"), ("data", "keyword"), ("data", "message_count"), ("data", "node_count"), ("data", "edge_count"), ("data", "nodes"), ("data", "edges"), ("data", "generated_at")), optional=(("data", "nodes", "[]", "metadata", "message_count"), ("data", "nodes", "[]", "metadata", "mention_count"), ("data", "edges", "[]", "metadata", "turn_count")), risk="high", reason="Summary is adapter-generated display text. Graph identity, scope, source, topology, counts and generation time are protected; topic/person counts live in optional node/edge metadata and there is no top-level media count.", status="allow"),
    _item("get_semantic_graph", output=(("data", "graph_id"), ("data", "group_id"), ("data", "scope"), ("data", "source_key"), ("data", "title"), ("data", "keyword"), ("data", "message_count"), ("data", "node_count"), ("data", "edge_count"), ("data", "nodes"), ("data", "nodes", "[]", "label"), ("data", "nodes", "[]", "kind"), ("data", "nodes", "[]", "weight"), ("data", "nodes", "[]", "metadata"), ("data", "nodes", "[]", "metadata", "message_count"), ("data", "nodes", "[]", "metadata", "mention_count"), ("data", "edges"), ("data", "edges", "[]", "source"), ("data", "edges", "[]", "source_kind"), ("data", "edges", "[]", "target"), ("data", "edges", "[]", "target_kind"), ("data", "edges", "[]", "relation"), ("data", "edges", "[]", "weight"), ("data", "edges", "[]", "metadata"), ("data", "edges", "[]", "metadata", "turn_count"), ("data", "updated_at"), ("data", "summary")), candidates=(("data", "summary"),), blocked=(("data", "graph_id"), ("data", "group_id"), ("data", "scope"), ("data", "source_key"), ("data", "title"), ("data", "keyword"), ("data", "message_count"), ("data", "node_count"), ("data", "edge_count"), ("data", "nodes"), ("data", "edges"), ("data", "updated_at")), optional=(("data", "nodes", "[]", "metadata", "message_count"), ("data", "nodes", "[]", "metadata", "mention_count"), ("data", "edges", "[]", "metadata", "turn_count")), risk="high", reason="Summary is adapter-regenerated display text. Persisted graph identity, scope, source, topology, counts and update time are protected.", status="allow"),
    _item("render_semantic_graph", output=(("data", "graph_id"), ("data", "group_id"), ("data", "image_filename"), ("data", "image_path"), ("data", "sent"), ("data", "send_error"), ("data", "summary")), candidates=(("data", "summary"),), blocked=(("data", "graph_id"), ("data", "group_id"), ("data", "image_filename"), ("data", "image_path"), ("data", "sent"), ("data", "send_error")), risk="high", reason="Summary is display text; file handles and delivery state are protected.", status="allow"),
)

OUTPUT_REDUCER_INVENTORY: Mapping[str, OutputReducerInventoryItem] = MappingProxyType(
    {item.tool_name: item for item in _ITEMS}
)


def list_output_reducer_inventory() -> tuple[OutputReducerInventoryItem, ...]:
    return tuple(OUTPUT_REDUCER_INVENTORY[name] for name in sorted(OUTPUT_REDUCER_INVENTORY))
