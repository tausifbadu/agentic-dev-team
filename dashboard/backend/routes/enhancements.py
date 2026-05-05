"""Enhancement request API routes -- add/update features on existing codebase."""

import sys
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import state_store
from agents.reasoning import restore_backup, list_backups
from workspace_paths import ensure_project_layout, normalize_project_id, resolve_workspace_dir
from dashboard.backend.execution import enhance_state, run_enhancement_background

router = APIRouter(tags=["enhancements"])


class EnhancementRequest(BaseModel):
    agent_type: str
    description: str
    context: str = ""
    project_id: str = "default"


@router.post("/enhance")
def submit_enhancement(req: EnhancementRequest):
    if req.agent_type not in ("backend", "frontend", "both"):
        raise HTTPException(400, f"Invalid agent_type: {req.agent_type}. Must be 'frontend', 'backend', or 'both'.")

    if enhance_state.get("running"):
        raise HTTPException(409, "An enhancement is already running. Wait for it to finish.")

    enhance_id = f"enh_{uuid.uuid4().hex[:8]}"
    pid = normalize_project_id(req.project_id)
    if pid != "default":
        ensure_project_layout(pid)
    state_store.save_enhancement(
        enhance_id=enhance_id,
        agent_type=req.agent_type,
        description=req.description,
        context=req.context,
        project_id=pid,
    )

    thread = threading.Thread(target=run_enhancement_background, args=(enhance_id,), daemon=True)
    thread.start()

    return {"enhance_id": enhance_id, "status": "pending", "project_id": pid}


@router.get("/enhance/status")
def get_enhance_status():
    return enhance_state.copy()


@router.get("/enhancements")
def list_enhancements(limit: int = 50):
    return state_store.list_enhancements(limit=limit)


@router.get("/enhancements/{enhance_id}")
def get_enhancement(enhance_id: str):
    enh = state_store.get_enhancement(enhance_id)
    if not enh:
        raise HTTPException(404, "Enhancement not found")
    return enh


@router.post("/enhancements/{enhance_id}/rollback")
def rollback_enhancement(enhance_id: str):
    enh = state_store.get_enhancement(enhance_id)
    if not enh:
        raise HTTPException(404, "Enhancement not found")

    if enhance_state.get("running"):
        raise HTTPException(409, "An enhancement is currently running. Wait for it to finish before rolling back.")

    bp = enh.get("backup_path", "")
    if not bp:
        raise HTTPException(400, "No backup available for this enhancement.")

    try:
        ws = resolve_workspace_dir(enh.get("project_id"))
        restore_backup(bp, ws)
        state_store.update_enhancement_status(enhance_id, "rolled_back", "Workspace restored from backup.")
        return {"status": "rolled_back", "message": f"Workspace restored from backup: {bp}"}
    except FileNotFoundError:
        raise HTTPException(404, f"Backup directory not found: {bp}")
    except Exception as exc:
        raise HTTPException(500, f"Rollback failed: {exc}")


@router.get("/backups")
def get_backups():
    return list_backups()
