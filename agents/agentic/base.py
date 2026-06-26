"""Shared base for class-based agentic runtime."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from openai import OpenAI

from agents.bus import BusMessage, MessageBus
from agents.react_loop import ReactLoopOutcome, run_react_loop
from agents.tools import build_default_registry
from agents.tools.registry import ToolContext, ToolRegistry
from schemas import Story


@dataclass
class Budget:
    """Resource caps shared across an agent's run.

    Tool-call and wall-clock caps stop runaway loops. Token caps are tracked
    coarsely (input + output chars / 4) when the LLM client cannot return usage.

    Env vars (read by `Budget.from_env()`):
      AGENTIC_MAX_TOOL_CALLS — total cap across all agents (default 600)
      AGENTIC_MAX_WALL_SECONDS — wall-clock cap for the run (default 1800)
    """

    max_tool_calls: int = 600
    max_wall_seconds: float = 1800.0
    started_at: float = field(default_factory=time.monotonic)
    tool_calls_used: int = 0
    tokens_used: int = 0

    @classmethod
    def from_env(cls) -> "Budget":
        return cls(
            max_tool_calls=int(os.getenv("AGENTIC_MAX_TOOL_CALLS", "600")),
            max_wall_seconds=float(os.getenv("AGENTIC_MAX_WALL_SECONDS", "1800")),
        )

    def tool_calls_exceeded(self) -> bool:
        return self.tool_calls_used >= self.max_tool_calls

    def wall_exceeded(self) -> bool:
        return (time.monotonic() - self.started_at) >= self.max_wall_seconds

    def remaining_seconds(self) -> float:
        return max(0.0, self.max_wall_seconds - (time.monotonic() - self.started_at))

    def snapshot(self) -> dict:
        return {
            "max_tool_calls": self.max_tool_calls,
            "max_wall_seconds": self.max_wall_seconds,
            "tool_calls_used": self.tool_calls_used,
            "elapsed_seconds": round(time.monotonic() - self.started_at, 1),
            "remaining_seconds": round(self.remaining_seconds(), 1),
        }


@dataclass
class RunContext:
    """All run-scoped values handed to every agent.

    The bus and budget are shared instances; per-agent isolation comes from
    `scratch_dir` and `tool_context` (each agent gets its own copy).
    """

    storypack_id: str
    run_id: str
    workspace_dir: Path
    bus: MessageBus
    budget: Budget = field(default_factory=Budget)
    requirement_text: str = ""
    all_stories: list[Story] = field(default_factory=list)


@dataclass
class RunResult:
    success: bool
    summary: str
    iterations: int = 0
    tool_calls: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------

ProgressCallback = Optional[Callable[[str, str, Optional[str]], None]]
"""(level, message, detail) callback used to stream progress to the dashboard."""


class AgentBase:
    """Common behaviour for every class-based agent."""

    agent_id: str = "base"
    role_description: str = "Generic agent"
    allowed_tools: list[str] = []
    workspace_subdir: Optional[str] = None  # "backend", "frontend", "tests" or None
    model_env_var: str = ""
    default_model: str = "codex/gpt-5.5"
    iteration_cap: int = 50

    def __init__(self, ctx: RunContext, on_progress: ProgressCallback = None):
        self.ctx = ctx
        self.on_progress = on_progress or (lambda level, msg, detail=None: None)
        self.bus = ctx.bus
        from agents.llm_client import make_openai_client
        self.client = make_openai_client()
        self.model = os.getenv(self.model_env_var, self.default_model) if self.model_env_var else self.default_model

        # Bus handler for incoming peer messages (must be installed by the caller
        # via `register_with_bus()` once the agent instance exists).
        self._current_story: Optional[Story] = None

    # ---------- Public API ----------

    def register_with_bus(self) -> None:
        self.bus.register_handler(self.agent_id, self._handle_bus_message)

    def unregister(self) -> None:
        self.bus.unregister_handler(self.agent_id)

    # Subclasses should override `run()` and may override `handle_message()`.

    def run(self, story: Story) -> RunResult:
        raise NotImplementedError

    def handle_message(self, msg: BusMessage) -> Optional[dict[str, Any]]:
        """Default: synthesize a short LLM-generated reply for `question` / `query` events."""
        if msg.event_type not in ("question", "query", "review_request"):
            return None
        question = (msg.payload or {}).get("question") or (msg.payload or {}).get("artifact") or ""
        if not question:
            return None
        from agents.llm_client import call_llm_text

        sys_prompt = (
            f"You are {self.role_description}. A peer agent asked you a question. "
            "Answer concisely (under 200 words). Return plain text."
        )
        try:
            answer = call_llm_text(
                self.client, self.model, sys_prompt, question,
                temperature=0.2, max_tokens=512,
            )
        except Exception as exc:  # noqa: BLE001
            answer = f"(error answering: {exc})"
        return {"answer": answer}

    # ---------- Internals ----------

    def _handle_bus_message(self, msg: BusMessage) -> Optional[dict[str, Any]]:
        try:
            return self.handle_message(msg)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    def _build_tool_context(self, story: Optional[Story], scratch_dir: Optional[Path]) -> ToolContext:
        ctx = ToolContext(
            agent_id=self.agent_id,
            storypack_id=self.ctx.storypack_id,
            run_id=self.ctx.run_id,
            story_id=story.id if story else None,
            workspace_dir=self.ctx.workspace_dir,
            scratch_dir=scratch_dir,
            bus=self.bus,
            budget=self.ctx.budget,
        )
        ctx.metadata["workspace_dir"] = str(self.ctx.workspace_dir)
        ctx.metadata["backend_dir"] = str(self.ctx.workspace_dir / "backend")
        return ctx

    def _build_registry(self, story: Optional[Story], scratch_dir: Optional[Path]) -> ToolRegistry:
        ctx = self._build_tool_context(story, scratch_dir)
        full = build_default_registry(ctx)
        if self.allowed_tools:
            return full.filter(self.allowed_tools)
        return full

    def _emit(self, level: str, message: str, detail: Optional[str] = None) -> None:
        try:
            self.on_progress(level, message, detail)
        except Exception:
            pass

    def _on_react_step(self, step: dict[str, Any]) -> None:
        text = (step.get("text") or "").strip()
        tool_calls = step.get("tool_calls") or []
        iteration = step.get("iteration", 0)
        if tool_calls:
            names = ", ".join(tc.get("name", "?") for tc in tool_calls)
            preview = "\n".join(
                f"{tc.get('name')}({_format_args(tc.get('arguments'))})" for tc in tool_calls
            )
            if len(preview) > _ARG_PREVIEW_MAX:
                preview = preview[:_ARG_PREVIEW_MAX] + "…"
            self._emit("info", f"[iter {iteration}] tool calls: {names}", preview)
        elif text:
            self._emit("info", f"[iter {iteration}] thinking: {text[:160]}")

    def _execute_react(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        registry: ToolRegistry,
        model: Optional[str] = None,
    ) -> ReactLoopOutcome:
        chosen = model or self.model
        if chosen != self.model:
            self._emit("info", f"Using per-story model override: {chosen}")
        return run_react_loop(
            client=self.client,
            model=chosen,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            registry=registry,
            max_iterations=self.iteration_cap,
            temperature=0.2,
            on_step=self._on_react_step,
        )

    def _story_model(self, story) -> Optional[str]:
        """Per-story model override (Story.model), if any."""
        return getattr(story, "model", None) if story is not None else None


# ---------------------------------------------------------------------------

# Live-log tool-call argument formatting. We show the actual VALUES passed (not
# just keys), but each value is truncated so a large arg (e.g. write_file content
# ~8KB) is previewed, not dumped, into agent_logs / the live console.
_ARG_VALUE_MAX = int(os.getenv("AGENTIC_LOG_ARG_VALUE_MAX", "300"))
_ARG_PREVIEW_MAX = int(os.getenv("AGENTIC_LOG_ARG_PREVIEW_MAX", "2400"))


def _format_args(args: Any) -> str:
    """Render tool-call arguments as `key=value, …` with each value truncated.

    Shows what was actually passed (the values), capped per-value so huge args
    (file contents, patches) are previewed rather than flooding the log.
    """
    if args is None:
        return ""
    if isinstance(args, str):
        try:
            data = json.loads(args)
        except Exception:  # noqa: BLE001
            return args[:_ARG_VALUE_MAX]
    elif isinstance(args, dict):
        data = args
    else:
        return str(args)[:_ARG_VALUE_MAX]
    if not isinstance(data, dict):
        return str(data)[:_ARG_VALUE_MAX]

    parts: list[str] = []
    for k, v in data.items():
        if isinstance(v, str):
            sval = v
        else:
            try:
                sval = json.dumps(v, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                sval = str(v)
        sval = sval.replace("\n", "\\n")
        if len(sval) > _ARG_VALUE_MAX:
            sval = sval[:_ARG_VALUE_MAX] + f"…(+{len(sval) - _ARG_VALUE_MAX} chars)"
        parts.append(f"{k}={sval}")
    return ", ".join(parts)
