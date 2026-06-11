"""StoryPack API routes."""

from pathlib import Path
from threading import Thread

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import sys
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import state_store
from dashboard.backend.execution import (
    expand_story_selection,
    execution_state,
    run_agents_background,
    completed_story_ids,
)
from schemas import Story

router = APIRouter(tags=["storypacks"])


class ApproveBody(BaseModel):
    fast_track: bool | None = None
    # Non-empty: run only these story ids (transitive prerequisites added automatically).
    story_ids: list[str] | None = None


class ResumeBody(BaseModel):
    fast_track: bool | None = None
    # Non-empty: resume only these story ids (+ their not-yet-done prerequisites).
    # Empty/None: run every story that hasn't completed yet.
    story_ids: list[str] | None = None


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
def approve_storypack(pack_id: str, body: ApproveBody = ApproveBody()):
    if execution_state.get("running"):
        raise HTTPException(status_code=409, detail="Pipeline is already running. Wait for it to finish.")

    pack = state_store.get_storypack(pack_id)
    if not pack:
        raise HTTPException(status_code=404, detail="StoryPack not found")
    if pack["status"] != "pending_review":
        raise HTTPException(status_code=400, detail=f"Cannot approve pack with status: {pack['status']}")

    if body.story_ids:
        full = [Story(**s) for s in pack["stories"]]
        try:
            expand_story_selection(full, body.story_ids)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    state_store.update_storypack_status(pack_id, "approved")
    state_store.add_agent_log(None, "orchestrator", f"StoryPack {pack_id} approved. Starting agents.")

    thread = Thread(
        target=run_agents_background,
        args=(pack_id,),
        kwargs={"fast_track": body.fast_track, "story_ids": body.story_ids},
        daemon=True,
    )
    thread.start()

    return {"status": "approved", "message": "Agents started in background."}


@router.post("/storypacks/{pack_id}/resume")
def resume_storypack(pack_id: str, body: ResumeBody = ResumeBody()):
    """Run the stories of a previously-run pack that haven't completed yet.

    Use after a partial run (e.g. backend-only) to do the rest, or to retry the
    failed stories. Already-completed stories are skipped, so dependencies that
    succeeded earlier are not re-run.
    """
    if execution_state.get("running"):
        raise HTTPException(status_code=409, detail="Pipeline is already running. Wait for it to finish.")

    pack = state_store.get_storypack(pack_id)
    if not pack:
        raise HTTPException(status_code=404, detail="StoryPack not found")
    if pack["status"] not in ("completed", "failed", "approved", "in_progress"):
        raise HTTPException(
            status_code=400,
            detail=(f"Cannot resume a pack with status '{pack['status']}'. "
                    "Use /approve for a pack pending review."),
        )

    full = [Story(**s) for s in pack["stories"]]
    if body.story_ids:
        try:
            selected, _ = expand_story_selection(full, body.story_ids)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        candidate_ids = {s.id for s in selected}
    else:
        candidate_ids = {s.id for s in full}

    done = completed_story_ids(pack_id)
    pending = [s.id for s in full if s.id in candidate_ids and s.id not in done]
    if not pending:
        return {"status": "noop", "message": "All selected stories are already completed.",
                "pending": [], "skipped_completed": sorted(candidate_ids & done)}

    state_store.update_storypack_status(pack_id, "in_progress")
    state_store.add_agent_log(
        None, "orchestrator",
        f"StoryPack {pack_id} resumed: {len(pending)} pending story(ies), "
        f"{len(candidate_ids & done)} already-completed skipped.",
    )

    thread = Thread(
        target=run_agents_background,
        args=(pack_id,),
        kwargs={"fast_track": body.fast_track, "story_ids": body.story_ids, "resume": True},
        daemon=True,
    )
    thread.start()

    return {"status": "resumed", "pending": pending,
            "skipped_completed": sorted(candidate_ids & done),
            "message": "Agents started in background (resume)."}


@router.post("/storypacks/{pack_id}/reject")
def reject_storypack(pack_id: str):
    pack = state_store.get_storypack(pack_id)
    if not pack:
        raise HTTPException(status_code=404, detail="StoryPack not found")

    state_store.update_storypack_status(pack_id, "rejected")
    state_store.add_agent_log(None, "orchestrator", f"StoryPack {pack_id} rejected.")
    return {"status": "rejected"}
