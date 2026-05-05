"""Tool registry. Tools are typed callables exposed to LLM agents via function calling.

A `Tool` wraps a Python handler with a JSON schema for its parameters. The registry
exposes:
  - `as_openai_schema()` — list of tool specs in OpenAI's function-calling shape.
  - `invoke(name, args)` — execute a tool by name with parsed JSON args.
  - `record_call()` — persisted to the `tool_calls` SQLite table for audit.

A shared `ToolContext` carries per-run state (story id, run id, scratch dir, message
bus reference, budgets) so handlers don't need positional context arguments.
"""

from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


class ToolError(Exception):
    """Raised when a tool handler fails. Surfaced back to the LLM as an error result."""


@dataclass
class ToolResult:
    """Structured tool invocation outcome."""

    ok: bool
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_text(self) -> str:
        if self.ok:
            return self.content
        return f"ERROR: {self.content}"


@dataclass
class ToolContext:
    """Per-run shared state passed to every tool handler.

    `bus` is the `MessageBus` instance (set by the agent runtime).
    `scratch_dir` is the agent's isolated working copy.
    `budget` tracks remaining tool calls and tokens.
    """

    agent_id: str
    storypack_id: str = ""
    run_id: str = ""
    story_id: Optional[str] = None
    workspace_dir: Optional[Path] = None
    scratch_dir: Optional[Path] = None
    bus: Any = None
    budget: Any = None
    tool_call_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Tool:
    """A registered tool with JSON-schema parameter spec and a Python handler."""

    name: str
    description: str
    parameters_schema: dict[str, Any]
    handler: Callable[[ToolContext, dict[str, Any]], ToolResult]

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }


class ToolRegistry:
    """Holds the set of tools available to an agent and dispatches invocations."""

    CIRCUIT_BREAKER_THRESHOLD = 5
    # After the breaker opens, allow auto-recovery (one fresh attempt) when the
    # agent has either:
    #   (a) modified files via write_file / apply_patch / delete_file (the cause
    #       of the prior failure may have been fixed), or
    #   (b) waited STALE_RECOVERY_CALLS unrelated tool calls without any file
    #       modifications (avoids permanent lockout for transient failures).
    STALE_RECOVERY_CALLS = 6
    _MUTATION_TOOLS = frozenset({"write_file", "apply_patch", "delete_file"})

    def __init__(self, context: ToolContext):
        self.context = context
        self._tools: dict[str, Tool] = {}
        self._consecutive_failures: dict[str, int] = {}
        # Per-tool: how many tool calls we've seen since the breaker opened, and
        # whether a mutation has happened in that window. Reset whenever we
        # half-open the breaker.
        self._calls_since_open: dict[str, int] = {}
        self._mutation_since_open: dict[str, bool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' already registered")
        self._tools[tool.name] = tool

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def as_openai_schema(self) -> list[dict[str, Any]]:
        return [t.to_openai_schema() for t in self._tools.values()]

    def filter(self, allow: Optional[list[str]] = None) -> "ToolRegistry":
        """Return a new registry containing only the named tools (preserving context)."""
        sub = ToolRegistry(context=self.context)
        for name in (allow or self.names()):
            if name in self._tools:
                sub._tools[name] = self._tools[name]
        return sub

    def invoke(self, name: str, args_json: str | dict[str, Any]) -> ToolResult:
        if name not in self._tools:
            return ToolResult(ok=False, content=f"Unknown tool: {name}")

        if isinstance(args_json, str):
            try:
                args = json.loads(args_json) if args_json else {}
            except json.JSONDecodeError as exc:
                return ToolResult(ok=False, content=f"Invalid JSON args for {name}: {exc}")
        else:
            args = dict(args_json or {})

        tool = self._tools[name]
        self.context.tool_call_count += 1
        if self.context.budget is not None:
            self.context.budget.tool_calls_used += 1

        # Budget enforcement (tool calls + wall clock).
        if self.context.budget is not None:
            if self.context.budget.tool_calls_exceeded():
                return ToolResult(
                    ok=False,
                    content=(
                        f"Tool budget exceeded for agent '{self.context.agent_id}'. "
                        "Stop and call finish_story now or escalate to PM."
                    ),
                )
            if self.context.budget.wall_exceeded():
                return ToolResult(
                    ok=False,
                    content=(
                        "Wall-clock budget exceeded for this run. "
                        "Stop and call finish_story now."
                    ),
                )

        # Track activity on every other tool so we can decide when to half-open
        # an OPEN breaker. A file mutation is a strong "the underlying problem
        # may now be fixed" signal; STALE_RECOVERY_CALLS unrelated calls is a
        # weak fallback so we don't lock the agent out forever.
        if name in self._MUTATION_TOOLS:
            for breaker_name in list(self._mutation_since_open.keys()):
                self._mutation_since_open[breaker_name] = True
        for breaker_name in list(self._calls_since_open.keys()):
            if breaker_name != name:
                self._calls_since_open[breaker_name] = (
                    self._calls_since_open.get(breaker_name, 0) + 1
                )

        # Circuit breaker: if a single tool fails N times consecutively, refuse
        # to run it again unless we have a recovery signal.
        if self._consecutive_failures.get(name, 0) >= self.CIRCUIT_BREAKER_THRESHOLD:
            had_mutation = self._mutation_since_open.get(name, False)
            stale_calls = self._calls_since_open.get(name, 0)
            recovered = had_mutation or stale_calls >= self.STALE_RECOVERY_CALLS
            if not recovered:
                return ToolResult(
                    ok=False,
                    content=(
                        f"Circuit breaker OPEN for tool '{name}' "
                        f"({self.CIRCUIT_BREAKER_THRESHOLD} consecutive failures). "
                        "Modify a relevant source file (write_file / apply_patch / delete_file) "
                        f"or take {self.STALE_RECOVERY_CALLS - stale_calls} different tool actions, "
                        "then retry. If you are out of ideas, call finish_story(success=false)."
                    ),
                )
            # Half-open: drop the count to threshold-1 so this run gets one
            # fresh attempt; if it fails, the breaker re-opens immediately.
            self._consecutive_failures[name] = self.CIRCUIT_BREAKER_THRESHOLD - 1
            self._mutation_since_open[name] = False
            self._calls_since_open[name] = 0

        started = time.monotonic()
        try:
            result = tool.handler(self.context, args)
            if not isinstance(result, ToolResult):
                result = ToolResult(ok=True, content=str(result))
        except ToolError as exc:
            result = ToolResult(ok=False, content=str(exc))
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc(limit=3)
            result = ToolResult(ok=False, content=f"{exc.__class__.__name__}: {exc}\n{tb}")

        elapsed_ms = int((time.monotonic() - started) * 1000)

        if result.ok:
            self._consecutive_failures[name] = 0
            self._calls_since_open.pop(name, None)
            self._mutation_since_open.pop(name, None)
        else:
            new_count = self._consecutive_failures.get(name, 0) + 1
            self._consecutive_failures[name] = new_count
            # Start tracking activity *as soon as* the breaker opens.
            if new_count >= self.CIRCUIT_BREAKER_THRESHOLD and name not in self._calls_since_open:
                self._calls_since_open[name] = 0
                self._mutation_since_open[name] = False

        self._record_call(name, args, result, elapsed_ms)
        return result

    def _record_call(
        self,
        name: str,
        args: dict[str, Any],
        result: ToolResult,
        elapsed_ms: int,
    ) -> None:
        try:
            import state_store

            state_store.save_tool_call(
                storypack_id=self.context.storypack_id or "none",
                run_id=self.context.run_id or "none",
                agent_id=self.context.agent_id,
                story_id=self.context.story_id,
                tool_name=name,
                args_json=_safe_json(args),
                ok=result.ok,
                result_excerpt=(result.content or "")[:2000],
                elapsed_ms=elapsed_ms,
            )
        except Exception:
            # Persistence is best-effort; never break the agent loop on logging.
            pass


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, default=str)[:8000]
    except Exception:  # noqa: BLE001
        return str(value)[:8000]
