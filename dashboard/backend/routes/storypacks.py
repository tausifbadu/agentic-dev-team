"""StoryPack API routes."""

import os
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

# Models the UI offers for per-story selection. Override via AGENTIC_AVAILABLE_MODELS
# (comma-separated). All must be gateway-valid "provider-slug/model-name" ids.
AVAILABLE_MODELS = [
    m.strip()
    for m in os.getenv(
        "AGENTIC_AVAILABLE_MODELS", "codex/gpt-5.4-mini,codex/gpt-5.4,codex/gpt-5.5"
    ).split(",")
    if m.strip()
]


@router.get("/models")
def list_models():
    """Models selectable per-story in the UI. 'default' = the agent's configured model."""
    return {"models": AVAILABLE_MODELS}


def _apply_story_models(pack_id: str, story_models: dict | None) -> None:
    """Persist per-story model overrides into the pack. Validates against the
    allow-list; an empty string / 'default' clears the override."""
    if not story_models:
        return
    for sid, model in story_models.items():
        model = (model or "").strip()
        if model in ("", "default"):
            state_store.update_story_model(pack_id, sid, None)
        elif model in AVAILABLE_MODELS:
            state_store.update_story_model(pack_id, sid, model)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown model '{model}' for {sid}")


class ApproveBody(BaseModel):
    fast_track: bool | None = None
    # Non-empty: run only these story ids (transitive prerequisites added automatically).
    story_ids: list[str] | None = None
    # When False, run EXACTLY story_ids without auto-adding prerequisites
    # (e.g. build just the UI shell without the backend chain).
    include_dependencies: bool = True
    # Optional per-story model overrides {story_id: model}. "" / "default" clears.
    story_models: dict[str, str] | None = None


class ResumeBody(BaseModel):
    fast_track: bool | None = None
    # Non-empty: resume only these story ids (+ their not-yet-done prerequisites).
    # Empty/None: run every story that hasn't completed yet.
    story_ids: list[str] | None = None
    include_dependencies: bool = True
    story_models: dict[str, str] | None = None
    # When True, re-run selected stories even if they already completed (overwrites
    # their prior result) instead of skipping them.
    force: bool = False


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


@router.get("/storypacks/{pack_id}/history")
def storypack_history(pack_id: str):
    """Append-only execution history (one entry per story run) for this pack —
    survives re-runs that overwrite the workspace + story status."""
    return {"runs": state_store.get_story_runs(storypack_id=pack_id)}


@router.get("/story-runs")
def story_runs(story_id: str | None = None, pack_id: str | None = None):
    """Execution history filtered by story and/or pack."""
    return {"runs": state_store.get_story_runs(storypack_id=pack_id, story_id=story_id)}


@router.get("/runs/{run_id}/iterations")
def run_iterations(run_id: str, story_id: str | None = None):
    """Per-iteration token usage for a run (optionally a single story)."""
    return {"iterations": state_store.get_iteration_usage(run_id, story_id)}


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
        ids = {s.id for s in full}
        if body.include_dependencies:
            try:
                expand_story_selection(full, body.story_ids)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        else:
            unknown = [sid for sid in body.story_ids if sid not in ids]
            if unknown:
                raise HTTPException(status_code=400, detail=f"Unknown story ids: {unknown}")

    _apply_story_models(pack_id, body.story_models)

    state_store.update_storypack_status(pack_id, "approved")
    state_store.add_agent_log(None, "orchestrator", f"StoryPack {pack_id} approved. Starting agents.")

    thread = Thread(
        target=run_agents_background,
        args=(pack_id,),
        kwargs={"fast_track": body.fast_track, "story_ids": body.story_ids,
                "include_dependencies": body.include_dependencies},
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
        if body.include_dependencies:
            try:
                selected, _ = expand_story_selection(full, body.story_ids)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            candidate_ids = {s.id for s in selected}
        else:
            ids = {s.id for s in full}
            unknown = [sid for sid in body.story_ids if sid not in ids]
            if unknown:
                raise HTTPException(status_code=400, detail=f"Unknown story ids: {unknown}")
            candidate_ids = set(body.story_ids)
    else:
        candidate_ids = {s.id for s in full}

    done = completed_story_ids(pack_id)
    if body.force:
        # Force: re-run selected stories even if already completed.
        pending = [s.id for s in full if s.id in candidate_ids]
        skipped = []
    else:
        pending = [s.id for s in full if s.id in candidate_ids and s.id not in done]
        skipped = sorted(candidate_ids & done)
    if not pending:
        return {"status": "noop", "message": "All selected stories are already completed.",
                "pending": [], "skipped_completed": skipped}

    _apply_story_models(pack_id, body.story_models)

    state_store.update_storypack_status(pack_id, "in_progress")
    state_store.add_agent_log(
        None, "orchestrator",
        f"StoryPack {pack_id} resumed: {len(pending)} pending story(ies)"
        + (f", {len(skipped)} already-completed skipped." if skipped
           else (" (force re-run, including completed)." if body.force else ".")),
    )

    thread = Thread(
        target=run_agents_background,
        args=(pack_id,),
        kwargs={"fast_track": body.fast_track, "story_ids": body.story_ids, "resume": True,
                "include_dependencies": body.include_dependencies, "force": body.force},
        daemon=True,
    )
    thread.start()

    return {"status": "resumed", "pending": pending,
            "skipped_completed": skipped,
            "message": "Agents started in background (resume)."}


@router.post("/storypacks/{pack_id}/reject")
def reject_storypack(pack_id: str):
    pack = state_store.get_storypack(pack_id)
    if not pack:
        raise HTTPException(status_code=404, detail="StoryPack not found")

    state_store.update_storypack_status(pack_id, "rejected")
    state_store.add_agent_log(None, "orchestrator", f"StoryPack {pack_id} rejected.")
    return {"status": "rejected"}
