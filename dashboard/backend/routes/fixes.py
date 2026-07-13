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
from agents.reasoning import promote_scratch_copy
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


# --- #4: recover verified work from runs that hit the iteration cap ------------

@router.get("/agents/recoverable-checkpoints")
def recoverable_checkpoints(status: str = "preserved", limit: int = 50):
    """List preserved scratch dirs from capped runs (verified=1 means promotable)."""
    return state_store.list_recoverable_checkpoints(status=status or None, limit=limit)


@router.post("/agents/recover-checkpoint/{checkpoint_id}")
def recover_checkpoint(checkpoint_id: int):
    """Promote a preserved checkpoint's scratch dir to its live target directory."""
    ck = state_store.get_recoverable_checkpoint(checkpoint_id)
    if not ck:
        raise HTTPException(404, "Checkpoint not found")
    if ck["status"] == "promoted":
        raise HTTPException(400, "Checkpoint already promoted")
    scratch = Path(ck["scratch_path"])
    target = Path(ck["target_dir"]) if ck.get("target_dir") else None
    if not scratch.exists():
        state_store.update_checkpoint_status(checkpoint_id, "missing")
        raise HTTPException(410, f"Scratch no longer exists: {scratch}")
    if target is None:
        raise HTTPException(400, "No target_dir recorded for this checkpoint")
    manifest = promote_scratch_copy(scratch, target, label=f"recover{checkpoint_id}")
    state_store.update_checkpoint_status(checkpoint_id, "promoted")
    return {
        "status": "promoted",
        "checkpoint_id": checkpoint_id,
        "verified": bool(ck.get("verified")),
        "target_dir": str(target),
        "manifest": manifest,
    }


class RollbackRequest(BaseModel):
    backup_path: str
    target_dir: str


@router.get("/agents/promote-backups")
def promote_backups(target_dir: str):
    """List the timestamped backups taken before promotes to a target directory."""
    td = Path(target_dir)
    root = td.parent / ".backups"
    if not root.exists():
        return []
    return sorted((str(d) for d in root.glob(f"{td.name}_*") if d.is_dir()), reverse=True)


@router.post("/agents/rollback-promote")
def rollback_promote(req: RollbackRequest):
    """Restore a target directory from one of its pre-promote backups (itself backed up first)."""
    bp = Path(req.backup_path)
    td = Path(req.target_dir)
    if not bp.exists():
        raise HTTPException(404, f"Backup not found: {bp}")
    manifest = promote_scratch_copy(bp, td, label="rollback")
    return {"status": "rolled_back", "target_dir": str(td), "manifest": manifest}
