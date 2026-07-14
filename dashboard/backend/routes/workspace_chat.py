"""Dashboard workspace chat — per-project, NDJSON streaming, optional file writes."""

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
from dashboard.backend.workspace_chat import (
    iter_workspace_chat_turn,
    release_workspace_chat_lock,
    try_acquire_workspace_chat_lock,
)

router = APIRouter(tags=["workspace-chat"])


class NewSessionBody(BaseModel):
    project_id: str = "default"


class MessageBody(BaseModel):
    session_id: str
    project_id: str = "default"
    message: str
    allow_writes: bool = False


@router.post("/workspace-chat/sessions")
def new_workspace_chat_session(body: NewSessionBody):
    sid = f"wschat_{uuid.uuid4().hex[:12]}"
    state_store.save_workspace_chat_session(sid, body.project_id)
    return {"session_id": sid, "project_id": body.project_id}


@router.post("/workspace-chat/message")
def stream_workspace_chat_message(body: MessageBody):
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="message is required")

    if not try_acquire_workspace_chat_lock():
        raise HTTPException(
            status_code=409,
            detail="Another workspace chat turn is in progress. Wait for it to finish.",
        )

    def ndjson_generator():
        try:
            state_store.save_workspace_chat_session(body.session_id, body.project_id)
            yield from iter_workspace_chat_turn(
                project_id=body.project_id.strip() or "default",
                session_id=body.session_id,
                user_message=body.message.strip(),
                allow_writes=body.allow_writes,
                lock_acquired=True,
            )
        finally:
            release_workspace_chat_lock()

    return StreamingResponse(ndjson_generator(), media_type="application/x-ndjson")


@router.get("/workspace-chat/edits")
def list_chat_edits(session_id: str | None = None, limit: int = 40):
    return {"edits": state_store.list_workspace_chat_edits(session_id=session_id, limit=limit)}


@router.get("/workspace-chat/usage")
def get_chat_usage(session_id: str):
    """Session token totals (persisted) for the token-burn display."""
    return state_store.get_workspace_chat_usage(session_id)
