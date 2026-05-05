"""Server-Sent Events stream powering the Live Console.

A single endpoint multiplexes three event types:

  - `comm`      — every persisted agent_comms row, pushed within ~50ms of
                  `MessageBus._persist()` when a supervisor is running, or
                  within 1s when polling the database fallback (used for idle
                  periods, late subscribers, and early reconnects).
  - `metrics`   — periodic snapshot every 2s while a run is active and every
                  5s otherwise. Same shape as `/api/agents/metrics`.
  - `heartbeat` — every 15s so proxies/firewalls don't kill the connection.

Reconnects are safe: the client passes `since_id=<last seen agent_comms.id>`
and the server replays anything newer before attaching to the live bus.

The route uses FastAPI's `StreamingResponse` with an asyncio queue. A lightweight
bridge thread is created so synchronous `MessageBus` handlers can push into the
asyncio queue without blocking the agent thread.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import state_store
from dashboard.backend.execution import (
    execution_state,
    get_active_bus,
)
from dashboard.backend.routes.agents import build_metrics_snapshot

router = APIRouter(tags=["events"])


# ---------------------------------------------------------------------------
# Helpers


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_metrics_snapshot() -> dict:
    """Single source of truth shared with the REST /agents/metrics endpoint."""
    return build_metrics_snapshot(recent_call_limit=20)


def _serialize_comm_row(row: dict) -> dict:
    """Normalize an agent_comms row for transmission."""
    return {
        "id": row["id"],
        "correlation_id": row.get("correlation_id") or "",
        "from_agent": row["from_agent"],
        "to_agent": row["to_agent"],
        "event_type": row["event_type"],
        "story_id": row.get("story_id"),
        "summary": row.get("summary") or "",
        "payload_json": row.get("payload_json") or "{}",
        "created_at": row["created_at"],
    }


def _bus_msg_to_row_shape(msg, comm_id: int = 0) -> dict:
    """Convert a BusMessage to the same shape as an agent_comms row."""
    to_agent = msg.to_agent if msg.to_agent != "<broadcast>" else f"topic:{msg.topic}"
    return {
        "id": comm_id,  # we don't know the real id when bus pushes; client treats <=0 as live
        "correlation_id": msg.correlation_id or "",
        "from_agent": msg.from_agent,
        "to_agent": to_agent,
        "event_type": msg.event_type,
        "story_id": msg.story_id,
        "summary": (msg.summary or "")[:200],
        "payload_json": json.dumps(msg.payload, default=str)[:8000],
        "created_at": _now_iso(),
    }


def _format_sse(event_id: Optional[int], payload: dict) -> bytes:
    """Encode a Server-Sent Events frame.

    Browsers expose `event_id` via `EventSource.onmessage(e => e.lastEventId)`
    and automatically include it as `Last-Event-ID` on reconnect.
    """
    lines = []
    if event_id is not None and event_id > 0:
        lines.append(f"id: {event_id}")
    lines.append(f"data: {json.dumps(payload, default=str)}")
    lines.append("")  # frame separator
    lines.append("")
    return ("\n".join(lines)).encode("utf-8")


# ---------------------------------------------------------------------------
# Stream


@router.get("/events/stream")
async def events_stream(request: Request, since_id: int = Query(0, ge=0)):
    """Open a long-lived SSE stream of comm events + metrics snapshots.

    Query params:
        since_id — last agent_comms.id the client already has; the server replays
                   anything newer before attaching to the live bus.
    """
    # Honor the standard Last-Event-ID header on reconnect.
    last_event_id = request.headers.get("last-event-id")
    if last_event_id:
        try:
            since_id = max(since_id, int(last_event_id))
        except ValueError:
            pass

    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=500)
    loop = asyncio.get_running_loop()
    last_seen_id = since_id
    stop_event = threading.Event()

    # Bridge from synchronous bus handlers into the asyncio queue.
    def _bus_listener(msg):
        try:
            row = _bus_msg_to_row_shape(msg)
            frame = _format_sse(None, {"type": "comm", "row": row, "live": True})
            loop.call_soon_threadsafe(_safe_put, frame)
        except Exception:
            pass

    def _safe_put(frame: bytes) -> None:
        try:
            queue.put_nowait(frame)
        except asyncio.QueueFull:
            pass

    # Attach to whatever bus is currently active. We re-check periodically
    # because supervisors may start mid-stream.
    attached_bus = None
    bus = get_active_bus()
    if bus is not None:
        try:
            bus.subscribe_all(_bus_listener)
            attached_bus = bus
        except Exception:
            attached_bus = None

    async def _reattach_loop() -> None:
        """Re-check for an active bus every 2s in case a run starts later."""
        nonlocal attached_bus
        while not stop_event.is_set():
            await asyncio.sleep(2.0)
            current = get_active_bus()
            if current is not None and current is not attached_bus:
                # Detach old, attach new.
                if attached_bus is not None:
                    try:
                        attached_bus.unsubscribe_all(_bus_listener)
                    except Exception:
                        pass
                try:
                    current.subscribe_all(_bus_listener)
                    attached_bus = current
                except Exception:
                    pass
            elif current is None and attached_bus is not None:
                # Run finished; detach.
                try:
                    attached_bus.unsubscribe_all(_bus_listener)
                except Exception:
                    pass
                attached_bus = None

    async def _db_tail_loop() -> None:
        """Poll agent_comms by id every 1s to cover idle periods + replay gap."""
        nonlocal last_seen_id
        while not stop_event.is_set():
            try:
                rows = state_store.get_agent_comms_since(last_seen_id, limit=200)
                for row in rows:
                    last_seen_id = max(last_seen_id, int(row["id"]))
                    payload = {"type": "comm", "row": _serialize_comm_row(row), "live": False}
                    _safe_put(_format_sse(int(row["id"]), payload))
            except Exception:
                pass
            await asyncio.sleep(1.0)

    async def _metrics_loop() -> None:
        """Push a metrics snapshot every 2s (running) or 5s (idle)."""
        while not stop_event.is_set():
            try:
                snap = _build_metrics_snapshot()
                _safe_put(_format_sse(None, {"type": "metrics", "snapshot": snap}))
                interval = 2.0 if execution_state.get("running") else 5.0
            except Exception:
                interval = 5.0
            await asyncio.sleep(interval)

    async def _heartbeat_loop() -> None:
        """Keep proxies from killing idle connections."""
        while not stop_event.is_set():
            await asyncio.sleep(15.0)
            _safe_put(_format_sse(None, {"type": "heartbeat", "ts": _now_iso()}))

    async def event_generator():
        # Replay any rows newer than since_id immediately so the client lands
        # on the latest state without holes.
        nonlocal last_seen_id
        try:
            replay = state_store.get_agent_comms_since(since_id, limit=500)
            for row in replay:
                last_seen_id = max(last_seen_id, int(row["id"]))
                payload = {"type": "comm", "row": _serialize_comm_row(row), "live": False}
                yield _format_sse(int(row["id"]), payload)
        except Exception:
            pass

        # Initial metrics snapshot.
        try:
            yield _format_sse(None, {"type": "metrics", "snapshot": _build_metrics_snapshot()})
        except Exception:
            pass

        tail = asyncio.create_task(_db_tail_loop())
        metrics = asyncio.create_task(_metrics_loop())
        heart = asyncio.create_task(_heartbeat_loop())
        reattach = asyncio.create_task(_reattach_loop())

        try:
            while True:
                # Stop streaming if the client disconnected.
                if await request.is_disconnected():
                    break
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=1.0)
                    yield frame
                except asyncio.TimeoutError:
                    continue
        finally:
            stop_event.set()
            for task in (tail, metrics, heart, reattach):
                task.cancel()
            if attached_bus is not None:
                try:
                    attached_bus.unsubscribe_all(_bus_listener)
                except Exception:
                    pass

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",  # disable buffering for nginx
    }
    return StreamingResponse(event_generator(), media_type="text/event-stream", headers=headers)
