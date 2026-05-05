"""Requirements API routes."""

import uuid
from pathlib import Path
from threading import Thread

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import sys
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import state_store
from schemas import Requirement, Story
from agents.pm_agent import create_stories
from workspace_paths import ensure_project_layout, normalize_project_id
from dashboard.backend.execution import execution_state, expand_story_selection, run_agents_background

router = APIRouter(tags=["requirements"])

PROMPT_DIR = PROJECT_ROOT / "prompt"


class RequirementCreate(BaseModel):
    text: str
    auto_approve: bool = False
    # When True with auto_approve, skip test + smoke (supervisor). None uses AGENTIC_FAST_TRACK env.
    fast_track: bool | None = None
    # Non-empty: run only these stories (+ transitive deps). Omit or empty = full pack.
    story_ids: list[str] | None = None
    # ``default`` uses repo ``workspace/``; other ids use ``workspace/projects/<id>/``.
    project_id: str = "default"


class RequirementFromFile(BaseModel):
    filename: str
    auto_approve: bool = False
    fast_track: bool | None = None
    story_ids: list[str] | None = None
    project_id: str = "default"


@router.post("/requirements")
def submit_requirement(body: RequirementCreate):
    req_id = f"req_{uuid.uuid4().hex[:8]}"
    state_store.save_requirement(req_id, body.text)

    pid = normalize_project_id(body.project_id)
    if pid != "default":
        ensure_project_layout(pid)

    pack = create_stories(Requirement(id=req_id, text=body.text))
    stories_raw = [s.model_dump() for s in pack.stories]

    initial_status = "pending_review"
    if body.auto_approve:
        if execution_state.get("running"):
            raise HTTPException(409, "Pipeline is already running. Cannot auto-approve now.")
        if body.story_ids:
            full_models = [Story(**s) for s in stories_raw]
            try:
                expand_story_selection(full_models, body.story_ids)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        initial_status = "approved"

    state_store.save_storypack(
        pack.id, req_id, body.text, stories_raw, initial_status,
        project_id=pid,
    )

    result = {
        "requirement_id": req_id,
        "storypack_id": pack.id,
        "stories": stories_raw,
        "status": initial_status,
        "project_id": pid,
    }

    if body.auto_approve:
        state_store.add_agent_log(None, "orchestrator",
                                  f"Auto-approved storypack {pack.id}. Starting agents.")
        thread = Thread(
            target=run_agents_background,
            args=(pack.id,),
            kwargs={"fast_track": body.fast_track, "story_ids": body.story_ids},
            daemon=True,
        )
        thread.start()
        result["message"] = "Auto-approved. Agents started in background."

    return result


@router.post("/requirements/from-file")
def submit_from_file(body: RequirementFromFile):
    path = Path(body.filename)
    if not path.exists():
        path = PROMPT_DIR / f"{body.filename}.txt"
    if not path.exists():
        path = PROMPT_DIR / body.filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Prompt file not found: {body.filename}")

    text = path.read_text(encoding="utf-8").strip()
    return submit_requirement(
        RequirementCreate(
            text=text,
            auto_approve=body.auto_approve,
            fast_track=body.fast_track,
            story_ids=body.story_ids,
            project_id=body.project_id,
        ),
    )


@router.get("/requirements")
def list_requirements():
    reqs = state_store.list_requirements()
    result = []
    for req in reqs:
        pack = state_store.get_storypack_by_requirement(req["id"])
        result.append({
            **req,
            "storypack_id": pack["id"] if pack else None,
            "storypack_status": pack["status"] if pack else None,
        })
    return result


@router.get("/requirements/{req_id}")
def get_requirement(req_id: str):
    req = state_store.get_requirement(req_id)
    if not req:
        raise HTTPException(status_code=404, detail="Requirement not found")
    pack = state_store.get_storypack_by_requirement(req_id)
    return {**req, "storypack": pack}


@router.get("/prompt-files")
def list_prompt_files():
    if not PROMPT_DIR.exists():
        return []
    files = sorted(PROMPT_DIR.glob("*.txt"))
    return [
        {
            "name": f.stem,
            "filename": f.name,
            "first_line": f.read_text(encoding="utf-8").split("\n", 1)[0].strip()[:80],
        }
        for f in files
    ]
