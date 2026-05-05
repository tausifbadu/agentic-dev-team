"""Agent Communications API routes."""

import json
from pathlib import Path

from fastapi import APIRouter, Query

import sys
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import state_store
from agents.reasoning import load_json

WORKSPACE = PROJECT_ROOT / "workspace"
CONTRACTS_DIR = WORKSPACE / "contracts"

router = APIRouter(tags=["comms"])


@router.get("/comms")
def get_comms(
    storypack_id: str | None = Query(None),
    run_id: str | None = Query(None),
    event_type: str | None = Query(None),
    from_agent: str | None = Query(None),
    to_agent: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
):
    return state_store.get_agent_comms(
        storypack_id=storypack_id,
        run_id=run_id,
        event_type=event_type,
        from_agent=from_agent,
        to_agent=to_agent,
        limit=limit,
    )


@router.get("/comms/timeline/{pack_id}")
def get_comm_timeline(pack_id: str):
    return state_store.get_comm_timeline(pack_id)


@router.get("/comms/contracts")
def get_contracts():
    path = CONTRACTS_DIR / "api_contract.json"
    if not path.exists():
        return {"routes": [], "models": []}
    return load_json(path, {"routes": [], "models": []})


@router.get("/comms/learning")
def get_learning_patterns(limit: int = Query(20, ge=1, le=100)):
    return state_store.get_failure_patterns(limit=limit)


@router.get("/comms/tool_calls")
def get_tool_calls(
    storypack_id: str | None = Query(None),
    run_id: str | None = Query(None),
    agent_id: str | None = Query(None),
    story_id: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
):
    """List individual tool invocations made by the new agentic runtime."""
    return state_store.list_tool_calls(
        storypack_id=storypack_id,
        run_id=run_id,
        agent_id=agent_id,
        story_id=story_id,
        limit=limit,
    )


@router.get("/comms/tool_calls/summary")
def get_tool_calls_summary(
    storypack_id: str | None = Query(None),
    run_id: str | None = Query(None),
):
    """Aggregated counts of tool usage per agent (for dashboard charts)."""
    return state_store.get_tool_call_summary(
        storypack_id=storypack_id, run_id=run_id,
    )


@router.get("/comms/inbox")
def get_inbox(
    to_agent: str | None = Query(None),
    run_id: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    """Durable inbox view of all agent-to-agent messages."""
    return state_store.list_inbox_messages(
        to_agent=to_agent, run_id=run_id, status=status, limit=limit,
    )
