"""Agent ReAct loop driver.

Each `*_agent.py` builds a system prompt, a tool registry, and a `ToolContext`,
then calls `run_react_loop()` which:

  1. Drives the LLM through `call_llm_with_tools()`.
  2. Routes tool calls into the registry via `ToolRegistry.invoke()`.
  3. Persists every assistant turn and tool result via the message bus + log.
  4. Stops when the agent calls `finish_story` (or hits a budget cap).

The result is a `ReactLoopOutcome` capturing whether the agent finished, how it
finished, and the structured `finish_story` payload it produced.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from openai import OpenAI

from .llm_client import ToolInvocation, ToolLoopResult, call_llm_with_tools
from .tools.registry import ToolRegistry, ToolContext


@dataclass
class ReactLoopOutcome:
    success: bool
    summary: str
    iterations: int
    final_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


def run_react_loop(
    *,
    client: OpenAI,
    model: str,
    system_prompt: str,
    user_prompt: str,
    registry: ToolRegistry,
    max_iterations: int = 25,
    temperature: float = 0.2,
    max_tokens: int = 6000,
    on_step: Optional[Callable[[dict[str, Any]], None]] = None,
) -> ReactLoopOutcome:
    """Run a complete ReAct loop for one agent task.

    The registry's `ToolContext` carries agent identity, scratch dir, and bus.
    The loop terminates when the agent invokes `finish_story` (registered via
    `register_agent_tools`) or when it produces a plain assistant message with no
    further tool calls.
    """
    ctx = registry.context
    tool_specs = registry.as_openai_schema()

    finish_signal: dict[str, Any] = {"hit": False}

    def _invoke(name: str, args_json: str) -> ToolInvocation:
        result = registry.invoke(name, args_json)
        terminate = bool(result.metadata.get("final"))
        if terminate:
            finish_signal["hit"] = True
            finish_signal["payload"] = ctx.metadata.get("finish", {})
        return ToolInvocation(
            content_for_model=result.to_text(),
            terminate=terminate,
        )

    def _is_finished(name: str, parsed: dict[str, Any]) -> bool:
        return name == "finish_story"

    started = time.monotonic()
    loop_result: ToolLoopResult = call_llm_with_tools(
        client=client,
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        tools=tool_specs,
        invoke_tool=_invoke,
        max_iterations=max_iterations,
        temperature=temperature,
        max_tokens=max_tokens,
        on_step=on_step,
        is_finished=_is_finished,
    )
    elapsed = time.monotonic() - started

    if loop_result.error:
        return ReactLoopOutcome(
            success=False,
            summary=f"Agent loop crashed after {loop_result.iterations} iterations: {loop_result.error}",
            iterations=loop_result.iterations,
            final_text=loop_result.final_text,
            error=loop_result.error,
            metadata={"elapsed_s": elapsed},
        )

    finish = ctx.metadata.get("finish") if finish_signal["hit"] else None
    if finish:
        return ReactLoopOutcome(
            success=bool(finish.get("success", True)),
            summary=finish.get("summary", "") or loop_result.final_text,
            iterations=loop_result.iterations,
            final_text=loop_result.final_text,
            metadata={"elapsed_s": elapsed, **(finish.get("metadata") or {})},
        )

    # Agent stopped without explicit finish — treat as success if it returned text,
    # failure if it ran out of iterations.
    if loop_result.iterations >= max_iterations and not loop_result.finished:
        return ReactLoopOutcome(
            success=False,
            summary=f"Agent exceeded {max_iterations} iterations without calling finish_story",
            iterations=loop_result.iterations,
            final_text=loop_result.final_text,
            metadata={"elapsed_s": elapsed},
        )

    return ReactLoopOutcome(
        success=True,
        summary=loop_result.final_text or "Loop completed without explicit finish",
        iterations=loop_result.iterations,
        final_text=loop_result.final_text,
        metadata={"elapsed_s": elapsed, "implicit_finish": True},
    )
