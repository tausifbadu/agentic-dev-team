"""In-process agent message bus with synchronous request/reply support.

The bus has three responsibilities:

  1. **Pub/Sub** — agents subscribe to topics (`story.assigned`, `contract.published`,
     ...) and react when events are published.

  2. **Direct messages** — `send(from_agent, to_agent, ...)` delivers a message to
     a specific agent's handler.

  3. **Synchronous request/reply** — `request(from, to, ...)` blocks the caller
     until the recipient's handler returns or until a timeout elapses. This is
     how agents have real bi-directional dialog (e.g. backend asks PM a question).

Every message is persisted to `agent_inbox` for durability and to `agent_comms`
for the dashboard timeline.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class BusMessage:
    """A message flowing through the bus."""

    id: str
    correlation_id: str
    from_agent: str
    to_agent: str
    event_type: str
    topic: str
    story_id: Optional[str]
    payload: dict[str, Any]
    summary: str
    created_at: float
    inbox_id: Optional[int] = None


@dataclass
class BusReply:
    """Reply emitted by an agent in response to a request."""

    correlation_id: str
    from_agent: str
    payload: dict[str, Any]
    summary: str = ""


# ---------- Subscription handlers ----------

DirectHandler = Callable[[BusMessage], Optional[dict[str, Any]]]
"""Handler that receives a direct message. May return a payload to use as auto-reply."""

TopicHandler = Callable[[BusMessage], None]
"""Handler that observes published events. Return value is ignored."""


class MessageBus:
    """Thread-safe in-process bus.

    Per-run identifiers (`storypack_id`, `run_id`) are stored on the bus so every
    persisted message is tagged for the dashboard.
    """

    def __init__(self, storypack_id: str = "", run_id: str = ""):
        self.storypack_id = storypack_id or "none"
        self.run_id = run_id or f"run_{uuid.uuid4().hex[:8]}"
        self._lock = threading.RLock()
        self._direct_handlers: dict[str, DirectHandler] = {}
        self._topic_handlers: dict[str, list[TopicHandler]] = defaultdict(list)
        self._all_handlers: list[TopicHandler] = []
        self._pending_replies: dict[str, "_PendingReply"] = {}
        self._closed = False
        self._messages: list[BusMessage] = []  # in-memory mirror for diagnostics

    # ---------- Registration ----------

    def register_handler(self, agent_id: str, handler: DirectHandler) -> None:
        """Bind an agent's direct-message handler. One handler per agent_id."""
        with self._lock:
            self._direct_handlers[agent_id] = handler

    def unregister_handler(self, agent_id: str) -> None:
        with self._lock:
            self._direct_handlers.pop(agent_id, None)

    def subscribe(self, topic: str, handler: TopicHandler) -> None:
        with self._lock:
            self._topic_handlers[topic].append(handler)

    def subscribe_all(self, handler: TopicHandler) -> None:
        """Receive every persisted message on the bus (used by the SSE stream).

        Handlers are best-effort and isolated: an exception in one will not
        affect other subscribers or the agent that emitted the message.
        """
        with self._lock:
            self._all_handlers.append(handler)

    def unsubscribe_all(self, handler: TopicHandler) -> None:
        with self._lock:
            try:
                self._all_handlers.remove(handler)
            except ValueError:
                pass

    # ---------- Direct send ----------

    def send(
        self,
        from_agent: str,
        to_agent: str,
        event_type: str,
        payload: Optional[dict[str, Any]] = None,
        story_id: Optional[str] = None,
        summary: str = "",
        correlation_id: Optional[str] = None,
    ) -> BusMessage:
        """Deliver a message to a specific agent (fire-and-forget)."""
        msg = self._build(
            from_agent=from_agent,
            to_agent=to_agent,
            event_type=event_type,
            topic="",
            payload=payload or {},
            story_id=story_id,
            summary=summary,
            correlation_id=correlation_id or "",
        )
        self._persist(msg)
        self._dispatch_direct(msg)
        return msg

    # ---------- Request / Reply ----------

    def request(
        self,
        from_agent: str,
        to_agent: str,
        event_type: str,
        payload: Optional[dict[str, Any]] = None,
        story_id: Optional[str] = None,
        summary: str = "",
        timeout: float = 60.0,
    ) -> Optional[dict[str, Any]]:
        """Send a message and block until the recipient replies or `timeout` seconds elapse.

        Returns the reply envelope `{from_agent, payload, summary}` or `None` on timeout.
        """
        correlation_id = uuid.uuid4().hex
        pending = _PendingReply(
            event=threading.Event(),
            requester=from_agent,
            event_type=event_type,
            story_id=story_id,
        )

        with self._lock:
            self._pending_replies[correlation_id] = pending

        # Compose a useful summary for the request side of the timeline.
        request_summary = summary or _summarize_request_payload(event_type, payload or {})

        msg = self._build(
            from_agent=from_agent,
            to_agent=to_agent,
            event_type=event_type,
            topic="",
            payload=payload or {},
            story_id=story_id,
            summary=request_summary,
            correlation_id=correlation_id,
        )
        self._persist(msg)

        # Dispatch synchronously; if the handler returns a payload directly, treat it as reply.
        sync_reply = self._dispatch_direct(msg)
        if sync_reply is not None:
            self.reply(correlation_id, to_agent, sync_reply)

        if not pending.event.wait(timeout=timeout):
            with self._lock:
                self._pending_replies.pop(correlation_id, None)
            self._persist_timeout(correlation_id, from_agent, to_agent, event_type, story_id, timeout)
            return None

        with self._lock:
            self._pending_replies.pop(correlation_id, None)

        return pending.reply

    def reply(self, correlation_id: str, from_agent: str,
              payload: dict[str, Any], summary: str = "") -> None:
        """Resolve a pending request with a structured payload."""
        with self._lock:
            pending = self._pending_replies.get(correlation_id)
        if not pending:
            return
        envelope = {
            "from_agent": from_agent,
            "payload": payload or {},
            "summary": summary or "",
            "correlation_id": correlation_id,
        }
        pending.reply = envelope
        pending.event.set()

        # Persist the reply addressed to the ORIGINAL requester so the timeline
        # shows a real "PM → Backend" exchange instead of "PM → <reply>".
        reply_summary = summary or _summarize_reply_payload(pending.event_type, payload or {})
        reply_event_type = f"{pending.event_type}_reply"

        reply_msg = self._build(
            from_agent=from_agent,
            to_agent=pending.requester,
            event_type=reply_event_type,
            topic="",
            payload=payload or {},
            story_id=pending.story_id,
            summary=reply_summary,
            correlation_id=correlation_id,
        )
        self._persist(reply_msg)

    # ---------- Pub/Sub ----------

    def publish(
        self,
        topic: str,
        from_agent: str,
        payload: Optional[dict[str, Any]] = None,
        story_id: Optional[str] = None,
        summary: str = "",
    ) -> BusMessage:
        # Use the topic itself as event_type so the dashboard can color/filter
        # `story.completed`, `run.started`, `contract.published`, etc. distinctly.
        msg = self._build(
            from_agent=from_agent,
            to_agent="<broadcast>",
            event_type=topic or "event",
            topic=topic,
            payload=payload or {},
            story_id=story_id,
            summary=summary,
            correlation_id="",
        )
        self._persist(msg)
        self._dispatch_topic(msg)
        return msg

    # ---------- Inspection ----------

    def messages(self) -> list[BusMessage]:
        with self._lock:
            return list(self._messages)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for pending in self._pending_replies.values():
                pending.event.set()
            self._pending_replies.clear()
            self._direct_handlers.clear()
            self._topic_handlers.clear()

    # ---------- Internals ----------

    def _build(self, **kwargs: Any) -> BusMessage:
        return BusMessage(
            id=uuid.uuid4().hex,
            correlation_id=kwargs.pop("correlation_id"),
            from_agent=kwargs.pop("from_agent"),
            to_agent=kwargs.pop("to_agent"),
            event_type=kwargs.pop("event_type"),
            topic=kwargs.pop("topic"),
            story_id=kwargs.pop("story_id"),
            payload=kwargs.pop("payload"),
            summary=kwargs.pop("summary"),
            created_at=time.time(),
        )

    def _persist(self, msg: BusMessage) -> None:
        with self._lock:
            self._messages.append(msg)

        # Store in agent_comms (timeline) and agent_inbox (durable queue).
        try:
            import state_store
            state_store.save_agent_comm(
                self.storypack_id, self.run_id,
                from_agent=msg.from_agent,
                to_agent=msg.to_agent if msg.to_agent != "<broadcast>" else f"topic:{msg.topic}",
                event_type=msg.event_type,
                story_id=msg.story_id,
                cycle_number=0,
                payload_json=json.dumps(msg.payload, default=str)[:8000],
                summary=msg.summary[:200],
                correlation_id=msg.correlation_id or "",
            )
            inbox_id = state_store.save_inbox_message(
                self.storypack_id, self.run_id,
                correlation_id=msg.correlation_id,
                from_agent=msg.from_agent,
                to_agent=msg.to_agent,
                event_type=msg.event_type,
                topic=msg.topic,
                story_id=msg.story_id,
                payload_json=json.dumps(msg.payload, default=str)[:8000],
                summary=msg.summary[:200],
            )
            msg.inbox_id = inbox_id
        except Exception:
            # Never break agent flow on persistence failures.
            pass

        # Fan out to subscribe_all handlers (SSE stream, future observers).
        # Snapshot under the lock so handlers can safely mutate the list.
        with self._lock:
            handlers = list(self._all_handlers)
        for h in handlers:
            try:
                h(msg)
            except Exception:
                pass

    def _persist_timeout(self, correlation_id: str, from_agent: str, to_agent: str,
                          event_type: str, story_id: Optional[str], timeout: float) -> None:
        """Record an unanswered request so the timeline shows the silence explicitly."""
        try:
            timeout_msg = self._build(
                from_agent=to_agent,
                to_agent=from_agent,
                event_type=f"{event_type}_timeout",
                topic="",
                payload={"timeout_seconds": timeout},
                story_id=story_id,
                summary=f"{to_agent} did not reply within {timeout:g}s",
                correlation_id=correlation_id,
            )
            self._persist(timeout_msg)
        except Exception:
            pass

    def _dispatch_direct(self, msg: BusMessage) -> Optional[dict[str, Any]]:
        """Deliver to the named handler if one is bound. Returns optional auto-reply."""
        with self._lock:
            handler = self._direct_handlers.get(msg.to_agent)
        if not handler:
            return None
        try:
            reply_payload = handler(msg)
        except Exception as exc:  # noqa: BLE001
            try:
                import state_store
                state_store.save_agent_comm(
                    self.storypack_id, self.run_id,
                    from_agent=msg.to_agent,
                    to_agent=msg.from_agent,
                    event_type="handler_error",
                    story_id=msg.story_id,
                    payload_json=json.dumps({"error": str(exc)})[:1000],
                    summary=f"Handler error: {exc}",
                )
            except Exception:
                pass
            return None

        try:
            import state_store
            if msg.inbox_id:
                state_store.update_inbox_status(msg.inbox_id, "delivered")
        except Exception:
            pass
        return reply_payload

    def _dispatch_topic(self, msg: BusMessage) -> None:
        with self._lock:
            handlers = list(self._topic_handlers.get(msg.topic, []))
        for handler in handlers:
            try:
                handler(msg)
            except Exception as exc:  # noqa: BLE001
                try:
                    import state_store
                    state_store.save_agent_comm(
                        self.storypack_id, self.run_id,
                        from_agent=f"topic:{msg.topic}",
                        to_agent="<observer>",
                        event_type="handler_error",
                        story_id=msg.story_id,
                        payload_json=json.dumps({"error": str(exc)})[:1000],
                        summary=f"Topic handler error: {exc}",
                    )
                except Exception:
                    pass


@dataclass
class _PendingReply:
    event: threading.Event
    requester: str = ""
    event_type: str = "question"
    story_id: Optional[str] = None
    reply: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Summary helpers — produce human-readable strings for the dashboard timeline
# so peer dialogs read naturally ("Backend asked PM: how should ...").

_TRUNC = 180


def _truncate(text: str, n: int = _TRUNC) -> str:
    text = (text or "").strip()
    if len(text) <= n:
        return text
    return text[:n - 1] + "…"


def _summarize_request_payload(event_type: str, payload: dict[str, Any]) -> str:
    if event_type in ("question", "query"):
        return f"asks: {_truncate(payload.get('question', ''))}"
    if event_type == "review_request":
        desc = payload.get("description") or ""
        artifact = payload.get("artifact") or ""
        return f"review requested: {_truncate(desc or artifact)}"
    if event_type == "heal_request":
        return f"heal cycle {payload.get('attempt', 1)}"
    if event_type == "rescope_request":
        return "asking PM to rescope after exhausted heals"
    if event_type == "triage_request":
        return "test triage requested"
    return _truncate(payload.get("content", "") or event_type)


def _summarize_reply_payload(event_type: str, payload: dict[str, Any]) -> str:
    if event_type in ("question", "query"):
        return f"answers: {_truncate(payload.get('answer', ''))}"
    if event_type == "review_request":
        verdict = payload.get("verdict", "approved")
        notes = payload.get("notes", "")
        return f"verdict={verdict}: {_truncate(notes)}"
    if event_type == "heal_request":
        return f"heal instructions: {_truncate(payload.get('instructions', ''))}"
    if event_type == "rescope_request":
        return f"decision={payload.get('decision', 'halt')}: {_truncate(payload.get('reason', ''))}"
    if event_type == "triage_request":
        return (
            f"target={payload.get('target_agent', '?')}: "
            f"{_truncate(payload.get('fix_instructions', ''))}"
        )
    return _truncate(payload.get("answer", "") or "(reply)")
