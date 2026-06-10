"""Agent ReAct loop driver.

Each `*_agent.py` builds a system prompt, a tool registry, and a `ToolContext`,
then calls `run_react_loop()` which:

  1. Drives the LLM through `call_llm_with_tools()`.
  2. Routes tool calls into the registry via `ToolRegistry.invoke()`.
  3. Persists every assistant turn and tool result via the message bus + log.
  4. Stops when the agent calls `finish_story` (or hits a budget cap).

Success semantics: the loop reports success ONLY when the agent explicitly called
`finish_story` and its Definition-of-Done gate accepted the call. Abandoning the
loop — running out of iterations, or returning a plain assistant message with no
tool call — is reported as a FAILURE, never an implicit success. This prevents
"false-green" stories that wrote no code and ran no validator from being recorded
as complete.

The result is a `ReactLoopOutcome` capturing whether the agent finished, how it
finished, and the structured `finish_story` payload it produced.
"""

from __future__ import annotations

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

    # NOTE: we deliberately do NOT pass an `is_finished` name predicate. Loop
    # termination is governed solely by `invocation.terminate`, which `_invoke`
    # derives from the tool result's `final` flag. The `finish_story` handler sets
    # `final` ONLY when its Definition-of-Done gate accepts the call (or on an
    # explicit success=false). A name-based predicate would short-circuit the loop
    # on *any* finish_story call — including one the gate rejected — silently
    # defeating the gate. Letting `terminate` drive things means a rejected
    # finish_story does not end the loop, so the agent gets the rejection message
    # and must re-validate.
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

    # No finish_story with an accepted Definition-of-Done gate was recorded.
    #
    # Success-semantics policy: a story is complete ONLY when the agent explicitly
    # calls finish_story AND the runtime DoD gate accepts it (which populates the
    # `finish` payload consumed above). Reaching this point means the agent
    # abandoned the loop — it ran out of iterations, or returned a plain assistant
    # message with no tool call. The previous implementation returned success=True
    # here ("implicit finish"), which silently passed stories that wrote no code and
    # ran no validator (the root cause of false-green stories). It is now a FAILURE,
    # surfaced to the supervisor (which publishes story.failed, records a failure
    # pattern, and asks PM to skip/simplify/halt).
    if loop_result.iterations >= max_iterations:
        summary = (
            f"Agent exhausted its {max_iterations}-iteration budget without a "
            "validated finish_story call. Marking the story failed for review."
        )
    else:
        summary = (
            "Agent ended its turn without calling finish_story (no validated "
            "Definition of Done). Marking the story failed for review."
        )
    return ReactLoopOutcome(
        success=False,
        summary=summary,
        iterations=loop_result.iterations,
        final_text=loop_result.final_text,
        metadata={"elapsed_s": elapsed, "implicit_finish_blocked": True},
    )
