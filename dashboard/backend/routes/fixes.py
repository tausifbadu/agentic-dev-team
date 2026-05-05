"""Fix request API routes -- debug feedback loop."""

import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import sys
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import state_store
from dashboard.backend.execution import fix_state, run_fix_background

router = APIRouter(tags=["fixes"])


class FixRequest(BaseModel):
    storypack_id: str
    story_id: str
    agent_type: str
    error_text: str
    user_instructions: str = ""


@router.post("/agents/fix")
def submit_fix(req: FixRequest):
    if req.agent_type not in ("backend", "frontend", "testing"):
        raise HTTPException(400, f"Invalid agent_type: {req.agent_type}")

    if fix_state.get("running"):
        raise HTTPException(409, "A fix is already running. Wait for it to finish.")

    fix_id = str(uuid.uuid4())
    state_store.save_fix_request(
        fix_id=fix_id,
        storypack_id=req.storypack_id,
        story_id=req.story_id,
        agent_type=req.agent_type,
        error_text=req.error_text,
        user_instructions=req.user_instructions,
    )

    thread = threading.Thread(target=run_fix_background, args=(fix_id,), daemon=True)
    thread.start()

    return {"fix_id": fix_id, "status": "pending"}


@router.get("/agents/fix/status")
def get_fix_status():
    return fix_state.copy()


@router.get("/agents/fixes")
def list_fixes(storypack_id: str | None = None, limit: int = 50):
    return state_store.list_fix_requests(storypack_id=storypack_id, limit=limit)


@router.get("/agents/fixes/{fix_id}")
def get_fix(fix_id: str):
    fix = state_store.get_fix_request(fix_id)
    if not fix:
        raise HTTPException(404, "Fix request not found")
    return fix
