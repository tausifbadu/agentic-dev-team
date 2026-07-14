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
from dashboard.backend.execution import (
    enhance_state,
    plan_enhancement_background,
    apply_enhancement_background,
    promote_enhancement,
    discard_enhancement,
    get_enhancement_diff,
)

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

    thread = threading.Thread(target=plan_enhancement_background, args=(enhance_id,), daemon=True)
    thread.start()

    return {"enhance_id": enhance_id, "status": "planning", "project_id": pid}


@router.post("/enhancements/{enhance_id}/approve")
def approve_enhancement(enhance_id: str):
    """Approve a reviewed plan and start applying it (backup -> agents -> promote)."""
    enh = state_store.get_enhancement(enhance_id)
    if not enh:
        raise HTTPException(404, "Enhancement not found")

    if enh.get("status") != "pending_review":
        raise HTTPException(
            409,
            f"Enhancement is '{enh.get('status')}', not awaiting review. Only a plan in "
            "'pending_review' can be approved.",
        )

    if enhance_state.get("running"):
        raise HTTPException(409, "An enhancement is already running. Wait for it to finish.")

    thread = threading.Thread(target=apply_enhancement_background, args=(enhance_id,), daemon=True)
    thread.start()

    return {"enhance_id": enhance_id, "status": "approved"}


@router.post("/enhancements/{enhance_id}/reject")
def reject_enhancement(enhance_id: str):
    """Reject a reviewed plan. Nothing was changed, so this just closes it out."""
    enh = state_store.get_enhancement(enhance_id)
    if not enh:
        raise HTTPException(404, "Enhancement not found")

    if enh.get("status") not in ("pending_review", "planning"):
        raise HTTPException(
            409,
            f"Enhancement is '{enh.get('status')}', not awaiting review. Only a pending plan "
            "can be rejected.",
        )

    state_store.update_enhancement_status(enhance_id, "rejected", "Plan rejected before any changes.")
    return {"enhance_id": enhance_id, "status": "rejected"}


@router.get("/enhancements/{enhance_id}/diff")
def enhancement_diff(enhance_id: str):
    """Return the staged (un-promoted) file-by-file diff for the diff gate."""
    enh = state_store.get_enhancement(enhance_id)
    if not enh:
        raise HTTPException(404, "Enhancement not found")
    return get_enhancement_diff(enhance_id)


@router.post("/enhancements/{enhance_id}/promote")
def promote_enhancement_route(enhance_id: str):
    """Diff gate approval: apply the staged changes to the live workspace."""
    enh = state_store.get_enhancement(enhance_id)
    if not enh:
        raise HTTPException(404, "Enhancement not found")

    if enh.get("status") != "pending_promote":
        raise HTTPException(
            409,
            f"Enhancement is '{enh.get('status')}', not awaiting promotion. Only staged changes "
            "in 'pending_promote' can be promoted.",
        )
    if enhance_state.get("running"):
        raise HTTPException(409, "An enhancement is currently running. Wait for it to finish.")

    res = promote_enhancement(enhance_id)
    if not res.get("ok"):
        raise HTTPException(500, f"Promote failed: {res.get('error')}")
    return {"enhance_id": enhance_id, "status": res.get("status", "success")}


@router.post("/enhancements/{enhance_id}/discard")
def discard_enhancement_route(enhance_id: str):
    """Diff gate rejection: throw away staged changes. Live workspace untouched."""
    enh = state_store.get_enhancement(enhance_id)
    if not enh:
        raise HTTPException(404, "Enhancement not found")

    if enh.get("status") != "pending_promote":
        raise HTTPException(
            409,
            f"Enhancement is '{enh.get('status')}', not awaiting promotion. Only staged changes "
            "in 'pending_promote' can be discarded.",
        )
    if enhance_state.get("running"):
        raise HTTPException(409, "An enhancement is currently running. Wait for it to finish.")

    res = discard_enhancement(enhance_id)
    if not res.get("ok"):
        raise HTTPException(500, f"Discard failed: {res.get('error')}")
    return {"enhance_id": enhance_id, "status": res.get("status", "discarded")}


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
