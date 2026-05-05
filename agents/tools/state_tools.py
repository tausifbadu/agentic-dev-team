"""State store tools — let agents read and write structured data."""

from __future__ import annotations

import json
from typing import Any

from .registry import Tool, ToolContext, ToolRegistry, ToolResult


def _read_storypack_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    import state_store

    pack_id = args.get("pack_id") or ctx.storypack_id
    if not pack_id:
        return ToolResult(ok=False, content="No pack_id available")

    pack = state_store.get_storypack(pack_id)
    if not pack:
        return ToolResult(ok=False, content=f"Storypack {pack_id} not found")
    return ToolResult(ok=True, content=json.dumps(pack, indent=2, default=str))


def _read_past_patterns_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    import state_store

    limit = int(args.get("limit", 10))
    patterns = state_store.get_failure_patterns(limit=limit)
    if not patterns:
        return ToolResult(ok=True, content="(no past failure patterns)")
    return ToolResult(ok=True, content=json.dumps(patterns, indent=2, default=str))


def _record_failure_pattern_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    import state_store

    storypack_id = ctx.storypack_id or "none"
    story_title = args.get("story_title", "unknown")
    agent_type = args.get("agent_type", ctx.agent_id)
    error_category = args.get("error_category", "")
    root_cause = args.get("root_cause", "")
    resolution = args.get("resolution", "")

    if not all([error_category, root_cause, resolution]):
        return ToolResult(
            ok=False,
            content="error_category, root_cause and resolution are required",
        )

    state_store.save_failure_pattern(
        storypack_id, story_title, agent_type, error_category, root_cause, resolution,
    )
    return ToolResult(ok=True, content=f"Recorded failure pattern: {error_category}")


def _read_logs_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    import state_store

    story_id = args.get("story_id") or ctx.story_id
    agent_type = args.get("agent_type")
    limit = int(args.get("limit", 30))
    logs = state_store.get_agent_logs(story_id=story_id, agent_type=agent_type, limit=limit)
    if not logs:
        return ToolResult(ok=True, content="(no logs)")
    summary = [
        f"[{log.get('created_at')}] {log.get('agent_type')}: {log.get('message')}"
        for log in logs
    ]
    return ToolResult(ok=True, content="\n".join(summary))


def _read_api_contract_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Frontend or test agent: fetch the latest published backend API contract."""
    from pathlib import Path

    workspace_dir = ctx.metadata.get("workspace_dir") or ""
    if not workspace_dir:
        return ToolResult(ok=False, content="No workspace_dir on context")
    contract_path = Path(workspace_dir) / "contracts" / "api_contract.json"
    if not contract_path.exists():
        return ToolResult(ok=True, content="(no API contract published yet)")
    try:
        text = contract_path.read_text(encoding="utf-8")
    except OSError as exc:
        return ToolResult(ok=False, content=f"Failed reading contract: {exc}")
    return ToolResult(ok=True, content=text)


def register_state_tools(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="read_storypack",
        description="Read the full storypack (all stories) for the current run.",
        parameters_schema={
            "type": "object",
            "properties": {"pack_id": {"type": "string"}},
            "additionalProperties": False,
        },
        handler=_read_storypack_handler,
    ))
    registry.register(Tool(
        name="read_past_patterns",
        description=(
            "Read failure patterns recorded from previous runs so you can avoid known mistakes."
        ),
        parameters_schema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
            "additionalProperties": False,
        },
        handler=_read_past_patterns_handler,
    ))
    registry.register(Tool(
        name="record_failure_pattern",
        description=(
            "Record a learned failure pattern (cross-run memory). PM agent should call this "
            "after a heal succeeds so future runs benefit."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "story_title": {"type": "string"},
                "agent_type": {"type": "string"},
                "error_category": {"type": "string"},
                "root_cause": {"type": "string"},
                "resolution": {"type": "string"},
            },
            "required": ["error_category", "root_cause", "resolution"],
            "additionalProperties": False,
        },
        handler=_record_failure_pattern_handler,
    ))
    registry.register(Tool(
        name="read_logs",
        description="Read recent agent log entries (any agent or filtered by agent_type/story_id).",
        parameters_schema={
            "type": "object",
            "properties": {
                "story_id": {"type": "string"},
                "agent_type": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        },
        handler=_read_logs_handler,
    ))
    registry.register(Tool(
        name="read_api_contract",
        description=(
            "Read the latest backend API contract (routes + models) published in workspace/contracts/api_contract.json."
        ),
        parameters_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_read_api_contract_handler,
    ))
