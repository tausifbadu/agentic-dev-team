"""StoryPack API routes."""

from pathlib import Path
from threading import Thread

from fastapi import APIRouter, HTTPException

import sys
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import state_store
from dashboard.backend.execution import execution_state, run_agents_background

router = APIRouter(tags=["storypacks"])


@router.get("/storypacks")
def list_storypacks():
    return state_store.list_storypacks()


@router.get("/storypacks/{pack_id}")
def get_storypack(pack_id: str):
    pack = state_store.get_storypack(pack_id)
    if not pack:
        raise HTTPException(status_code=404, detail="StoryPack not found")
    return pack


@router.get("/storypacks/{pack_id}/stories")
def get_stories(pack_id: str):
    pack = state_store.get_storypack(pack_id)
    if not pack:
        raise HTTPException(status_code=404, detail="StoryPack not found")
    return pack["stories"]


@router.post("/storypacks/{pack_id}/approve")
def approve_storypack(pack_id: str):
    if execution_state.get("running"):
        raise HTTPException(status_code=409, detail="Pipeline is already running. Wait for it to finish.")

    pack = state_store.get_storypack(pack_id)
    if not pack:
        raise HTTPException(status_code=404, detail="StoryPack not found")
    if pack["status"] != "pending_review":
        raise HTTPException(status_code=400, detail=f"Cannot approve pack with status: {pack['status']}")

    state_store.update_storypack_status(pack_id, "approved")
    state_store.add_agent_log(None, "orchestrator", f"StoryPack {pack_id} approved. Starting agents.")

    thread = Thread(target=run_agents_background, args=(pack_id,), daemon=True)
    thread.start()

    return {"status": "approved", "message": "Agents started in background."}


@router.post("/storypacks/{pack_id}/reject")
def reject_storypack(pack_id: str):
    pack = state_store.get_storypack(pack_id)
    if not pack:
        raise HTTPException(status_code=404, detail="StoryPack not found")

    state_store.update_storypack_status(pack_id, "rejected")
    state_store.add_agent_log(None, "orchestrator", f"StoryPack {pack_id} rejected.")
    return {"status": "rejected"}
