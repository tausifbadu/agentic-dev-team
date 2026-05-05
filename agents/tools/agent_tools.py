"""Inter-agent communication tools.

These tools let one agent talk to another via the shared `MessageBus`. Two
patterns are supported:
  - Fire-and-forget publish: `send_message`, `publish_event`.
  - Synchronous request/reply: `ask_pm`, `request_review`, `query_agent`.

Every interaction is persisted to `agent_comms` so the dashboard timeline shows
true cross-agent dialog (not just orchestrator narration).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .registry import Tool, ToolContext, ToolError, ToolRegistry, ToolResult


def _bus(ctx: ToolContext):
    if ctx.bus is None:
        raise ToolError("No message bus configured for this agent")
    return ctx.bus


def _send_message_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    to_agent = args.get("to_agent", "")
    content = args.get("content", "")
    event_type = args.get("event_type", "message")
    if not to_agent or not content:
        return ToolResult(ok=False, content="'to_agent' and 'content' are required")

    bus = _bus(ctx)
    bus.send(
        from_agent=ctx.agent_id,
        to_agent=to_agent,
        event_type=event_type,
        payload={"content": content},
        story_id=ctx.story_id,
        summary=content[:120],
    )
    return ToolResult(ok=True, content=f"Sent to {to_agent}: {content[:120]}")


def _publish_event_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    topic = args.get("topic", "")
    payload = args.get("payload", {})
    summary = args.get("summary", "")
    if not topic:
        return ToolResult(ok=False, content="'topic' is required")
    if not isinstance(payload, dict):
        return ToolResult(ok=False, content="'payload' must be an object")

    bus = _bus(ctx)
    bus.publish(
        topic=topic,
        from_agent=ctx.agent_id,
        payload=payload,
        story_id=ctx.story_id,
        summary=summary or topic,
    )
    return ToolResult(ok=True, content=f"Published {topic}")


ASK_PM_MAX_PER_STORY = 3


def _ask_pm_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Synchronous question to the PM agent. PM responds via the same bus.

    Anti-loop guard: an agent may only ask PM up to ``ASK_PM_MAX_PER_STORY`` times
    per story. Beyond that, the tool returns a synthetic "PM has already advised"
    message instead of calling the LLM, forcing the agent to either fix the
    problem itself or call ``finish_story(success=false)`` with a clear summary.
    """
    question = args.get("question", "")
    if not question:
        return ToolResult(ok=False, content="'question' is required")

    count = int(ctx.metadata.get("ask_pm_count", 0))
    if count >= ASK_PM_MAX_PER_STORY:
        return ToolResult(
            ok=True,
            content=(
                f"PM has already advised on this story {count} times. "
                "PM will not answer further code-level questions. "
                "Either: (a) FIX THE CODE yourself by running validators "
                "(run_pytest / http_check / run_npm_build) and iterating on the "
                "actual error message, or (b) call finish_story(success=false) "
                "with a clear summary of what is blocking you and the exact "
                "error you cannot resolve. Do NOT call ask_pm again for this story."
            ),
            metadata={"ask_pm_capped": True},
        )

    bus = _bus(ctx)
    timeout = float(args.get("timeout", 60))
    reply = bus.request(
        from_agent=ctx.agent_id,
        to_agent="pm",
        event_type="question",
        payload={"question": question, "story_id": ctx.story_id},
        timeout=timeout,
    )
    ctx.metadata["ask_pm_count"] = count + 1

    if reply is None:
        return ToolResult(
            ok=False,
            content=f"PM did not reply within {timeout}s. Proceed with best judgment.",
        )
    answer = (reply.get("payload") or {}).get("answer") or reply.get("summary") or ""
    suffix = ""
    if count + 1 == ASK_PM_MAX_PER_STORY:
        suffix = (
            f"\n\n[anti-loop notice] You have now used {ASK_PM_MAX_PER_STORY}/{ASK_PM_MAX_PER_STORY} "
            "PM questions for this story. Subsequent ask_pm calls will be auto-rejected. "
            "Fix the code and run validators yourself from this point on."
        )
    elif count + 1 == ASK_PM_MAX_PER_STORY - 1:
        suffix = (
            "\n\n[anti-loop notice] One more ask_pm call is allowed for this story. "
            "After that you must rely on validators (run_pytest / http_check / run_npm_build) "
            "and your own debugging — do not bring code-level errors to PM."
        )
    return ToolResult(ok=True, content=(answer or "(empty answer)") + suffix)


def _request_review_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Ask another agent to review an artifact (e.g. backend asks PM to confirm contract)."""
    to_agent = args.get("to_agent", "pm")
    artifact = args.get("artifact", "")
    description = args.get("description", "")
    if not artifact:
        return ToolResult(ok=False, content="'artifact' is required")

    bus = _bus(ctx)
    timeout = float(args.get("timeout", 60))
    reply = bus.request(
        from_agent=ctx.agent_id,
        to_agent=to_agent,
        event_type="review_request",
        payload={"artifact": artifact, "description": description, "story_id": ctx.story_id},
        timeout=timeout,
    )
    if reply is None:
        return ToolResult(ok=True, content=f"No review reply from {to_agent}; proceeding.")
    verdict = (reply.get("payload") or {}).get("verdict", "approved")
    notes = (reply.get("payload") or {}).get("notes", "")
    return ToolResult(ok=True, content=f"Review verdict from {to_agent}: {verdict}\n{notes}")


def _query_agent_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Synchronous query to any agent (e.g. frontend asks backend for an endpoint shape)."""
    to_agent = args.get("to_agent", "")
    question = args.get("question", "")
    if not to_agent or not question:
        return ToolResult(ok=False, content="'to_agent' and 'question' are required")

    bus = _bus(ctx)
    timeout = float(args.get("timeout", 30))
    reply = bus.request(
        from_agent=ctx.agent_id,
        to_agent=to_agent,
        event_type="query",
        payload={"question": question, "story_id": ctx.story_id},
        timeout=timeout,
    )
    if reply is None:
        return ToolResult(
            ok=False,
            content=f"{to_agent} did not respond within {timeout}s",
        )
    answer = (reply.get("payload") or {}).get("answer") or reply.get("summary") or ""
    return ToolResult(ok=True, content=answer)


def _publish_contract_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Backend agent: publish the API contract derived from current backend code."""
    from agents.reasoning import extract_api_contract, save_json

    backend_dir = Path(ctx.metadata.get("backend_dir") or "")
    if not backend_dir.exists():
        return ToolResult(ok=False, content="backend_dir not configured on agent context")

    contract = extract_api_contract(backend_dir)
    contracts_dir = backend_dir.parent / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    save_json(contracts_dir / "api_contract.json", contract)

    bus = _bus(ctx)
    bus.publish(
        topic="contract.published",
        from_agent=ctx.agent_id,
        payload=contract,
        story_id=ctx.story_id,
        summary=f"{len(contract.get('routes', []))} routes, {len(contract.get('models', []))} models",
    )
    return ToolResult(
        ok=True,
        content=(
            f"Published API contract: {len(contract.get('routes', []))} routes, "
            f"{len(contract.get('models', []))} models"
        ),
        metadata={"contract": contract},
    )


_BACKEND_REQUIRED_VALIDATORS = {"run_pytest", "http_check"}
_FRONTEND_REQUIRED_VALIDATORS = {"run_npm_build"}


def _finish_story_handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Mark the agent's current story as complete and return the final answer.

    Definition-of-Done gate: ``success=true`` is rejected unless the agent has a
    recent passing **strong** validation and has not edited any files since.

    A "strong" validation differs by agent:
      - backend: ``run_pytest`` or ``http_check`` (smoke_uvicorn alone is NOT
        sufficient — it only proves the server boots, not that endpoints work).
      - frontend: ``run_npm_build``.
      - test: any passing validation, or none if the agent only authored tests.
    Agents that genuinely cannot proceed must still call ``finish_story`` but with
    ``success=false`` and a clear summary — that path is always allowed.
    """
    summary = args.get("summary", "Story completed")
    success = bool(args.get("success", True))
    metadata = args.get("metadata", {}) or {}
    if not isinstance(metadata, dict):
        metadata = {}

    if success:
        last_validation = ctx.metadata.get("last_validation")
        dirty = bool(ctx.metadata.get("dirty_since_validation"))

        if ctx.agent_id == "backend":
            required = _BACKEND_REQUIRED_VALIDATORS
            required_label = "run_pytest or http_check"
        elif ctx.agent_id == "frontend":
            required = _FRONTEND_REQUIRED_VALIDATORS
            required_label = "run_npm_build"
        else:
            required = set()
            required_label = ""

        if required:
            if not last_validation or not last_validation.get("ok"):
                return ToolResult(
                    ok=False,
                    content=(
                        "DoD gate: cannot finish_story(success=true) without a passing validation. "
                        f"Run {required_label} first; only call finish_story after it returns ok=True. "
                        "If you genuinely cannot make validation pass, call finish_story with "
                        "success=false and a clear failure summary instead."
                    ),
                )
            last_tool = last_validation.get("tool", "")
            if last_tool not in required:
                return ToolResult(
                    ok=False,
                    content=(
                        f"DoD gate: the most recent passing validation was '{last_tool}', but "
                        f"this story requires {required_label}. "
                        f"smoke_uvicorn alone proves only that the server boots; it does NOT "
                        f"prove the endpoints behave correctly. "
                        f"Run {required_label} and ensure it passes before calling finish_story."
                    ),
                )
            if dirty:
                return ToolResult(
                    ok=False,
                    content=(
                        f"DoD gate: you have edited files since the last successful {last_tool}. "
                        "Re-run the validator and try finish_story again."
                    ),
                )

    bus = _bus(ctx)
    topic = "story.completed" if success else "story.failed"
    bus.publish(
        topic=topic,
        from_agent=ctx.agent_id,
        payload={"summary": summary, **metadata},
        story_id=ctx.story_id,
        summary=summary[:200],
    )
    ctx.metadata["finish"] = {"success": success, "summary": summary, "metadata": metadata}
    return ToolResult(ok=True, content=f"FINISHED {topic}: {summary}", metadata={"final": True, "success": success})


def register_agent_tools(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="send_message",
        description="Send a free-form message to another agent (fire-and-forget).",
        parameters_schema={
            "type": "object",
            "properties": {
                "to_agent": {"type": "string", "enum": ["pm", "backend", "frontend", "test", "supervisor"]},
                "content": {"type": "string"},
                "event_type": {"type": "string"},
            },
            "required": ["to_agent", "content"],
            "additionalProperties": False,
        },
        handler=_send_message_handler,
    ))
    registry.register(Tool(
        name="publish_event",
        description="Publish an event on a bus topic so any subscribed agent can react.",
        parameters_schema={
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "payload": {"type": "object"},
                "summary": {"type": "string"},
            },
            "required": ["topic"],
            "additionalProperties": False,
        },
        handler=_publish_event_handler,
    ))
    registry.register(Tool(
        name="ask_pm",
        description=(
            "Ask the PM agent a clarifying question (synchronous). "
            "Use when the story is ambiguous or you hit a decision the spec doesn't cover."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "timeout": {"type": "number", "minimum": 5, "maximum": 300},
            },
            "required": ["question"],
            "additionalProperties": False,
        },
        handler=_ask_pm_handler,
    ))
    registry.register(Tool(
        name="request_review",
        description="Ask another agent (default PM) to review an artifact you produced.",
        parameters_schema={
            "type": "object",
            "properties": {
                "to_agent": {"type": "string"},
                "artifact": {"type": "string"},
                "description": {"type": "string"},
                "timeout": {"type": "number", "minimum": 5, "maximum": 300},
            },
            "required": ["artifact"],
            "additionalProperties": False,
        },
        handler=_request_review_handler,
    ))
    registry.register(Tool(
        name="query_agent",
        description="Ask any agent a synchronous question and wait for their reply.",
        parameters_schema={
            "type": "object",
            "properties": {
                "to_agent": {"type": "string"},
                "question": {"type": "string"},
                "timeout": {"type": "number", "minimum": 5, "maximum": 300},
            },
            "required": ["to_agent", "question"],
            "additionalProperties": False,
        },
        handler=_query_agent_handler,
    ))
    registry.register(Tool(
        name="publish_contract",
        description=(
            "Extract the API contract (routes + Pydantic models) from the current backend code "
            "and publish it on the bus so the frontend agent can see endpoint shapes."
        ),
        parameters_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_publish_contract_handler,
    ))
    registry.register(Tool(
        name="finish_story",
        description=(
            "Signal that you have completed (or definitively failed) the assigned story. "
            "After this returns, the runtime will stop your ReAct loop. "
            "Set success=false if you cannot proceed even with help."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "summary": {"type": "string"},
                "metadata": {"type": "object"},
            },
            "required": ["summary"],
            "additionalProperties": False,
        },
        handler=_finish_story_handler,
    ))
