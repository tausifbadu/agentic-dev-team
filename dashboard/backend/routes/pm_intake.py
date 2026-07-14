"""PM requirement-intake API — an interactive clarify -> revise -> confirm dialogue
that runs BEFORE story creation. NDJSON streaming, mirrors workspace-chat's shape.

On finalize it feeds the agreed (refined) requirement into the SAME create_stories()
+ save_storypack() the direct path uses, so the existing Story Board picks it up
unchanged."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import state_store
from schemas import Requirement
from agents.pm_agent import create_stories
from workspace_paths import ensure_project_layout, normalize_project_id
from dashboard.backend.pm_intake import (
    iter_pm_intake_turn,
    release_intake_lock,
    synthesize_refined_requirement,
    try_acquire_intake_lock,
)

router = APIRouter(tags=["pm-intake"])


class StartIntakeBody(BaseModel):
    text: str
    project_id: str = "default"


class IntakeMessageBody(BaseModel):
    session_id: str
    message: str


class FinalizeBody(BaseModel):
    # Optional user-edited spec. When omitted, uses the session's stored refined_text,
    # synthesizing one on the fly if none exists yet.
    refined_text: str | None = None


@router.post("/requirements/intake")
def start_intake(body: StartIntakeBody):
    """Create the requirement + intake session. Does NOT create stories yet.
    The client should then send the raw requirement as the first intake message."""
    if not body.text.strip():
        raise HTTPException(400, "requirement text is required")

    req_id = f"req_{uuid.uuid4().hex[:8]}"
    state_store.save_requirement(req_id, body.text.strip())

    pid = normalize_project_id(body.project_id)
    if pid != "default":
        ensure_project_layout(pid)

    sid = f"pmintake_{uuid.uuid4().hex[:12]}"
    state_store.save_pm_intake_session(sid, req_id, pid)
    return {"session_id": sid, "requirement_id": req_id, "project_id": pid}


@router.post("/requirements/intake/message")
def stream_intake_message(body: IntakeMessageBody):
    """Stream one PM turn (NDJSON)."""
    if not body.message.strip():
        raise HTTPException(400, "message is required")
    if not state_store.get_pm_intake_session(body.session_id):
        raise HTTPException(404, "intake session not found")

    if not try_acquire_intake_lock(body.session_id):
        raise HTTPException(409, "A turn for this intake is already in progress. Wait for it to finish.")

    def ndjson_generator():
        try:
            yield from iter_pm_intake_turn(
                session_id=body.session_id,
                user_message=body.message.strip(),
                lock_acquired=True,
            )
        finally:
            release_intake_lock(body.session_id)

    return StreamingResponse(ndjson_generator(), media_type="application/x-ndjson")


@router.get("/requirements/intake/{session_id}")
def get_intake(session_id: str):
    sess = state_store.get_pm_intake_session(session_id)
    if not sess:
        raise HTTPException(404, "intake session not found")
    return sess


@router.get("/requirements/intake/{session_id}/messages")
def get_intake_messages(session_id: str, limit: int = 400):
    """Serve the persisted transcript so a dialogue can be reloaded/revisited."""
    sess = state_store.get_pm_intake_session(session_id)
    if not sess:
        raise HTTPException(404, "intake session not found")
    return {
        "session": sess,
        "messages": state_store.list_pm_intake_messages(session_id, limit=limit),
    }


@router.post("/requirements/intake/{session_id}/synthesize")
def synthesize_intake(session_id: str):
    """Consolidate the conversation into a refined requirement spec (for the editable
    preview panel). Persists it on the session."""
    if not state_store.get_pm_intake_session(session_id):
        raise HTTPException(404, "intake session not found")
    try:
        spec = synthesize_refined_requirement(session_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Failed to synthesize spec: {exc}")
    return {"session_id": session_id, "refined_text": spec}


@router.post("/requirements/intake/{session_id}/finalize")
def finalize_intake(session_id: str, body: FinalizeBody):
    """Confirm the intake: turn the agreed requirement into stories via the SAME
    create_stories()/save_storypack() the direct path uses, then hand off to the
    existing Story Board (return storypack_id)."""
    sess = state_store.get_pm_intake_session(session_id)
    if not sess:
        raise HTTPException(404, "intake session not found")
    if sess.get("status") == "confirmed" and sess.get("storypack_id"):
        # Already finalized — return the existing pack instead of creating a duplicate.
        pack = state_store.get_storypack(sess["storypack_id"])
        return {
            "requirement_id": sess["requirement_id"],
            "storypack_id": sess["storypack_id"],
            "stories": (pack or {}).get("stories", []),
            "status": (pack or {}).get("status", "pending_review"),
            "project_id": sess.get("project_id", "default"),
            "already_finalized": True,
        }

    refined = (body.refined_text or "").strip() or (sess.get("refined_text") or "").strip()
    if not refined:
        try:
            refined = synthesize_refined_requirement(session_id).strip()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"Failed to synthesize requirement: {exc}")
    if not refined:
        raise HTTPException(400, "No requirement content to build from.")

    req_id = sess["requirement_id"]
    pid = normalize_project_id(sess.get("project_id", "default"))
    if pid != "default":
        ensure_project_layout(pid)

    # The refined spec becomes the storypack's requirement_text (what agents consume);
    # the original raw ask stays intact in the requirements row for audit.
    try:
        pack = create_stories(Requirement(id=req_id, text=refined))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Story generation failed: {exc}")

    stories_raw = [s.model_dump() for s in pack.stories]
    state_store.save_storypack(pack.id, req_id, refined, stories_raw, "pending_review", project_id=pid)
    state_store.update_pm_intake_session(
        session_id, status="confirmed", refined_text=refined, storypack_id=pack.id,
    )

    return {
        "requirement_id": req_id,
        "storypack_id": pack.id,
        "stories": stories_raw,
        "status": "pending_review",
        "project_id": pid,
    }
