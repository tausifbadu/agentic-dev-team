"""PM requirement intake: an interactive clarify -> revise -> confirm dialogue that
precedes story creation. NDJSON event stream, mirroring workspace_chat's shape but
with no filesystem tools — the PM only asks questions and, on request, synthesizes a
consolidated requirement spec that feeds the existing create_stories().
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    BadRequestError,
)

import state_store
from agents.llm_client import make_openai_client

MODEL_NAME = os.getenv("PM_INTAKE_MODEL", os.getenv("PM_AGENT_MODEL", "codex/gpt-5.5"))

_MAX_RETRIES = int(os.getenv("PM_INTAKE_MAX_RETRIES", "5"))
_BACKOFF_BASE = float(os.getenv("PM_INTAKE_BACKOFF_BASE", "3"))
_BACKOFF_MAX = float(os.getenv("PM_INTAKE_BACKOFF_MAX", "30"))
_HISTORY_LIMIT = 40      # sliding window of prior turns replayed as context
_MSG_CLIP = 12000        # per-message char cap (no gateway caching -> keep it bounded)


# ── Per-session locking ──
# The dialogue is read-only (no fs writes), so concurrent sessions are safe. We only
# serialize turns WITHIN one session so its message rows never interleave.
_locks_guard = threading.Lock()
_session_locks: dict[str, threading.Lock] = {}


def _lock_for(session_id: str) -> threading.Lock:
    with _locks_guard:
        lk = _session_locks.get(session_id)
        if lk is None:
            lk = threading.Lock()
            _session_locks[session_id] = lk
        return lk


def try_acquire_intake_lock(session_id: str) -> bool:
    return _lock_for(session_id).acquire(blocking=False)


def release_intake_lock(session_id: str) -> None:
    lk = _lock_for(session_id)
    if lk.locked():
        try:
            lk.release()
        except RuntimeError:
            pass


# ── Gateway resilience (mirrors workspace_chat) ──

def _backoff(attempt: int) -> float:
    return min(_BACKOFF_BASE * (2 ** (attempt - 1)), _BACKOFF_MAX)


def _robust_chat(client, **kwargs: Any):
    """chat.completions.create with retry on 429/5xx/transport/empty-choices and the
    temperature-unsupported fallback. Raises after the budget so the turn surfaces a
    clean error instead of dying on the first throttle."""
    omit_temp = False
    attempts = 0
    while True:
        call_kw = dict(kwargs)
        if omit_temp:
            call_kw.pop("temperature", None)
        try:
            resp = client.chat.completions.create(**call_kw)
        except BadRequestError as exc:
            err = str(exc).lower()
            if not omit_temp and "temperature" in err and (
                "unsupported" in err or "only the default" in err or "default (1)" in err
            ):
                omit_temp = True
                continue
            raise
        except APIStatusError as exc:
            status = getattr(exc, "status_code", 0) or 0
            if (status == 429 or status >= 500) and attempts < _MAX_RETRIES:
                attempts += 1
                time.sleep(_backoff(attempts))
                continue
            raise
        except (APIConnectionError, APITimeoutError):
            if attempts < _MAX_RETRIES:
                attempts += 1
                time.sleep(_backoff(attempts))
                continue
            raise
        if not getattr(resp, "choices", None):
            if attempts < _MAX_RETRIES:
                attempts += 1
                time.sleep(_backoff(attempts))
                continue
            raise RuntimeError("LLM gateway returned no choices after retries")
        return resp


def _extract_usage(resp: Any) -> dict[str, int]:
    u = getattr(resp, "usage", None)
    if u is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cached_tokens": 0}
    pt = int(getattr(u, "prompt_tokens", 0) or 0)
    ct = int(getattr(u, "completion_tokens", 0) or 0)
    cached = 0
    det = getattr(u, "prompt_tokens_details", None)
    if det is not None:
        cached = int(getattr(det, "cached_tokens", 0) or 0)
    return {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct, "cached_tokens": cached}


# ── Prompts ──

PM_SYSTEM_PROMPT = """You are a senior product manager running a requirement intake.
Your job is to turn a raw feature request into a crisp, buildable requirement THROUGH
CONVERSATION. You do NOT write user stories, tasks, or code here.

How to behave:
- Read the request and the conversation so far.
- Ask focused clarifying questions to resolve ambiguity: target users, the core user
  flows, data/entities, the key acceptance criteria, important edge cases, what is
  explicitly out of scope, and any hard constraints (auth, integrations, tech).
- Ask in SMALL batches — at most 3-4 questions per turn. Never dump a long
  questionnaire. Prefer concrete, closed questions and propose a sensible default the
  user can simply accept ("Should this require login? (default: yes)").
- As the picture firms up, briefly reflect back what you now understand.
- When you have enough to build a clear requirement, SAY SO explicitly and invite the
  user to confirm (they will click "Create stories"). Stop asking once things are clear.
- Keep every message tight and skimmable: short sentences, short bullets. Plain text
  only — no JSON, no code fences."""

SYNTHESIS_SYSTEM_PROMPT = """You are a product manager consolidating a
requirement-intake conversation into a single, self-contained requirement
specification a development team can build from directly. You are given the original
request and the full Q&A.

Produce a clear spec in plain markdown, using these sections where applicable:
- Summary (1-2 sentences)
- Users & goals
- Core flows / functionality
- Data & entities
- Acceptance criteria (bulleted, testable)
- Out of scope
- Constraints & assumptions

Fold in every decision reached in the conversation. Where the user did not specify
something, state a reasonable assumption explicitly rather than leaving it open. Do
NOT write user stories, tasks, or code — only the consolidated requirement."""


def _out(obj: dict[str, Any]) -> str:
    return json.dumps(obj, default=str) + "\n"


def iter_pm_intake_turn(
    *, session_id: str, user_message: str, lock_acquired: bool = False,
) -> Iterator[str]:
    """Yield NDJSON events for one PM intake turn (one LLM call — a question or a
    reflected-back summary). No tools, so a turn is a single request/response."""
    try:
        state_store.touch_pm_intake_session(session_id)
        state_store.add_pm_intake_message(session_id, "user", user_message)

        hist = state_store.list_pm_intake_messages(session_id, limit=_HISTORY_LIMIT)
        messages: list[dict[str, Any]] = [{"role": "system", "content": PM_SYSTEM_PROMPT}]
        for row in hist[:-1]:
            if row["role"] in ("user", "assistant"):
                messages.append({"role": row["role"], "content": row["content"][-_MSG_CLIP:]})
        messages.append({"role": "user", "content": user_message})

        client = make_openai_client()

        yield _out({"type": "meta", "session_id": session_id})
        try:
            resp = _robust_chat(client, model=MODEL_NAME, messages=messages, temperature=0.4)
        except Exception as exc:  # noqa: BLE001
            yield _out({"type": "error", "message": str(exc)})
            yield _out({"type": "done", "ok": False, "reason": "error"})
            return

        usage = _extract_usage(resp)
        yield _out({"type": "usage", "scope": "turn", "calls": 1, **usage})

        content = (resp.choices[0].message.content or "").strip()
        if not content:
            content = "(no response — please try again)"
        state_store.add_pm_intake_message(session_id, "assistant", content)
        yield _out({"type": "assistant", "content": content})
        yield _out({"type": "done", "ok": True, "reason": "final"})
    finally:
        if lock_acquired:
            release_intake_lock(session_id)


def synthesize_refined_requirement(session_id: str) -> str:
    """Consolidate the whole intake conversation into one requirement spec, persist it
    on the session as refined_text, and return it."""
    sess = state_store.get_pm_intake_session(session_id)
    if not sess:
        raise ValueError("intake session not found")

    original = ""
    req = state_store.get_requirement(sess.get("requirement_id", ""))
    if req:
        original = req.get("text", "") or ""

    hist = state_store.list_pm_intake_messages(session_id, limit=400)
    transcript = "\n\n".join(
        f"{r['role'].upper()}: {r['content']}"
        for r in hist if r["role"] in ("user", "assistant")
    )
    if not original:
        # Fall back to the first user turn (the raw request) if the row is missing.
        first_user = next((r["content"] for r in hist if r["role"] == "user"), "")
        original = first_user

    user_prompt = (
        f"ORIGINAL REQUEST:\n{original}\n\n"
        f"INTAKE CONVERSATION:\n{transcript}\n\n"
        "Produce the consolidated requirement specification now."
    )
    client = make_openai_client()
    resp = _robust_chat(
        client, model=MODEL_NAME,
        messages=[
            {"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
    )
    spec = (resp.choices[0].message.content or "").strip()
    if spec:
        state_store.update_pm_intake_session(session_id, refined_text=spec)
    return spec
