"""Agent status and logs API routes."""

from pathlib import Path

from fastapi import APIRouter

import sys
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import state_store
from dashboard.backend.execution import (
    execution_state,
    get_active_budget_snapshot,
    current_run_status,
    derive_budget_from_db,
)

router = APIRouter(tags=["agents"])


# All agents that participate in the pipeline. Always rendered in the
# per-agent panel even when their tool/message counters are zero so the
# operator can see PM activity even though it interacts purely via the bus.
KNOWN_AGENTS = ("pm", "backend", "frontend", "test")


def build_metrics_snapshot(*, recent_call_limit: int = 50) -> dict:
    """Compose the unified metrics payload used by the REST endpoint and
    the SSE Live Console stream. Combines tool calls + bus message counts so
    every known agent appears even when it only communicates via messages.
    """
    status = current_run_status()
    pack_id = status.get("storypack_id") or ""
    run_id = status.get("run_id") or ""

    summary = state_store.get_tool_call_summary(
        storypack_id=pack_id or None, run_id=run_id or None,
    )
    recent_calls = state_store.list_tool_calls(
        storypack_id=pack_id or None, run_id=run_id or None, limit=recent_call_limit,
    )
    msg_counts = state_store.get_agent_message_counts(
        storypack_id=pack_id or None, run_id=run_id or None,
    )

    # Pre-seed the panel with every known agent so PM (which usually has 0
    # tool calls but lots of messages) is always visible.
    by_agent: dict = {
        agent: {
            "agent_id": agent,
            "tool_calls": 0,
            "ok_calls": 0,
            "total_ms": 0,
            "tools_used": [],
            "messages_sent": int(msg_counts.get(agent, {}).get("sent", 0)),
            "messages_received": int(msg_counts.get(agent, {}).get("received", 0)),
        }
        for agent in KNOWN_AGENTS
    }
    # Also include any agent that surfaces through tool calls or messages but
    # isn't in KNOWN_AGENTS (e.g. supervisor).
    for row in summary:
        agent = row["agent_id"]
        if agent not in by_agent:
            by_agent[agent] = {
                "agent_id": agent, "tool_calls": 0, "ok_calls": 0, "total_ms": 0,
                "tools_used": [],
                "messages_sent": int(msg_counts.get(agent, {}).get("sent", 0)),
                "messages_received": int(msg_counts.get(agent, {}).get("received", 0)),
            }
    for agent in msg_counts.keys():
        if agent not in by_agent:
            by_agent[agent] = {
                "agent_id": agent, "tool_calls": 0, "ok_calls": 0, "total_ms": 0,
                "tools_used": [],
                "messages_sent": int(msg_counts[agent].get("sent", 0)),
                "messages_received": int(msg_counts[agent].get("received", 0)),
            }

    for row in summary:
        agent = row["agent_id"]
        d = by_agent[agent]
        d["tool_calls"] += int(row.get("count", 0))
        d["ok_calls"] += int(row.get("ok_count", 0))
        d["total_ms"] += int(row.get("total_ms", 0))
        d["tools_used"].append({
            "tool": row["tool_name"],
            "count": int(row.get("count", 0)),
        })

    live_budget = get_active_budget_snapshot()
    total_calls = sum(int(r.get("count", 0)) for r in summary)
    budget = (
        live_budget
        or status.get("budget")
        or execution_state.get("budget")
        or (derive_budget_from_db(pack_id, run_id or None, total_calls) if pack_id else {})
    )

    failures_by_tool: dict[tuple[str, str], int] = {}
    for row in recent_calls:
        key = (row["agent_id"], row["tool_name"])
        if not row["ok"]:
            failures_by_tool[key] = failures_by_tool.get(key, 0) + 1
        else:
            failures_by_tool[key] = 0
    breaker_warnings = [
        {"agent_id": k[0], "tool_name": k[1], "consecutive_failures": v}
        for k, v in failures_by_tool.items() if v >= 3
    ]

    # Sort by tool_calls desc, then messages desc, so the busiest agents float
    # to the top while still showing zeroed-out agents below.
    agents_sorted = sorted(
        by_agent.values(),
        key=lambda a: (
            -a.get("tool_calls", 0),
            -(a.get("messages_sent", 0) + a.get("messages_received", 0)),
            a.get("agent_id", ""),
        ),
    )

    return {
        "execution_state": status,
        "by_agent": agents_sorted,
        "recent_calls": recent_calls,
        "total_tool_calls": sum(a["tool_calls"] for a in agents_sorted),
        "total_messages": sum(
            (a.get("messages_sent", 0) + a.get("messages_received", 0))
            for a in agents_sorted
        ),
        "budget": budget,
        "breaker_warnings": breaker_warnings,
    }


@router.get("/agents/status")
def get_agent_status():
    return current_run_status()


@router.get("/agents/logs")
def get_all_logs(limit: int = 100):
    return state_store.get_agent_logs(limit=limit)


@router.get("/agents/logs/{story_id}")
def get_story_logs(story_id: str, limit: int = 50):
    return state_store.get_agent_logs(story_id=story_id, limit=limit)


@router.get("/agents/metrics")
def get_agent_metrics():
    """Live metrics for the agentic runtime: tool counts per agent,
    budget snapshot, and active run state."""
    return build_metrics_snapshot()
