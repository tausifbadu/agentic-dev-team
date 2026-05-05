"""Shared LLM client that supports Chat Completions, Completions, and Responses APIs.

Chat models (gpt-4o, gpt-4o-mini, gpt-4.1, o3-mini, etc.) use /v1/chat/completions.
Legacy completions models (davinci, babbage, cushman) use /v1/completions.
Responses-API models (gpt-5.3-codex, codex-mini, etc.) use /v1/responses.

This module also exposes `call_llm_with_tools()` — a ReAct loop driver used by the
agent runtime. The model emits tool calls, the host executes them, results are fed
back, and the loop continues until the model returns a final text answer or the
agent calls the `finish_story` tool.
"""

import json
import re
from typing import Any, Callable, Optional
from openai import OpenAI

RESPONSES_MODEL_PATTERNS = ("codex",)
LEGACY_COMPLETIONS_MODEL_PATTERNS = ("davinci", "babbage", "cushman")


def _model_api(model_name: str) -> str:
    """Return 'responses', 'completions', or 'chat' based on the model name."""
    lower = model_name.lower()
    if any(pat in lower for pat in RESPONSES_MODEL_PATTERNS):
        return "responses"
    if any(pat in lower for pat in LEGACY_COMPLETIONS_MODEL_PATTERNS):
        return "completions"
    return "chat"


def is_completions_model(model_name: str) -> bool:
    """Kept for backward compatibility — True for legacy completions models only."""
    return _model_api(model_name) == "completions"


def is_responses_model(model_name: str) -> bool:
    return _model_api(model_name) == "responses"


def _extract_json(text: str) -> str:
    """Strip markdown fences and leading/trailing noise to isolate JSON."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
        text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    return text


def call_llm_text(
    client: OpenAI,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> str:
    """Call an LLM and return plain text. Works with chat, completions, and responses APIs."""
    api = _model_api(model)

    if api == "responses":
        response = client.responses.create(
            model=model,
            instructions=system_prompt,
            input=user_prompt,
            temperature=temperature,
            max_output_tokens=max_tokens,
        )
        return (response.output_text or "").strip()

    if api == "completions":
        response = client.completions.create(
            model=model,
            prompt=f"{system_prompt}\n\n{user_prompt}",
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return (response.choices[0].text or "").strip()

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return (response.choices[0].message.content or "").strip()


def call_llm_json(
    client: OpenAI,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.2,
    max_tokens: int = 16384,
    max_retries: int = 2,
    retry_prompt: str = "Your previous response was not valid JSON. Return ONLY valid JSON matching the requested schema. No markdown fences. No commentary.",
    on_raw_response=None,
) -> dict:
    """Call an LLM and parse JSON response. Supports chat, completions, and responses APIs."""
    api = _model_api(model)

    if api == "responses":
        return _call_responses_json(
            client, model, system_prompt, user_prompt,
            temperature, max_tokens, max_retries, retry_prompt, on_raw_response,
        )
    if api == "completions":
        return _call_completions_json(
            client, model, system_prompt, user_prompt,
            temperature, max_tokens, max_retries, retry_prompt, on_raw_response,
        )
    return _call_chat_json(
        client, model, system_prompt, user_prompt,
        temperature, max_tokens, max_retries, retry_prompt, on_raw_response,
    )


def _call_chat_json(
    client, model, system_prompt, user_prompt,
    temperature, max_tokens, max_retries, retry_prompt, on_raw_response,
) -> dict:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    errors: list[str] = []
    for _ in range(max_retries + 1):
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            response_format={"type": "json_object"},
            max_tokens=max_tokens,
        )
        content = (response.choices[0].message.content or "").strip()
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            errors.append(str(exc))
            if on_raw_response:
                on_raw_response(content)
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": retry_prompt})
    raise ValueError(f"Invalid JSON from {model} (chat) after retries: " + " | ".join(errors))


def _call_completions_json(
    client, model, system_prompt, user_prompt,
    temperature, max_tokens, max_retries, retry_prompt, on_raw_response,
) -> dict:
    prompt = f"""{system_prompt}

{user_prompt}

Respond with valid JSON only. No markdown fences. No commentary."""

    errors: list[str] = []
    for _ in range(max_retries + 1):
        response = client.completions.create(
            model=model,
            prompt=prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        content = (response.choices[0].text or "").strip()
        extracted = _extract_json(content)
        try:
            return json.loads(extracted)
        except json.JSONDecodeError as exc:
            errors.append(str(exc))
            if on_raw_response:
                on_raw_response(content)
            prompt = f"{prompt}\n\nAssistant: {content}\n\n{retry_prompt}\n\nRespond with valid JSON only."
    raise ValueError(f"Invalid JSON from {model} (completions) after retries: " + " | ".join(errors))


def _call_responses_json(
    client, model, system_prompt, user_prompt,
    temperature, max_tokens, max_retries, retry_prompt, on_raw_response,
) -> dict:
    """Use the Responses API (/v1/responses) with JSON-object output format."""
    input_messages = [
        {"role": "user", "content": user_prompt + "\n\nRespond with valid JSON only."},
    ]
    errors: list[str] = []
    for _ in range(max_retries + 1):
        response = client.responses.create(
            model=model,
            instructions=system_prompt,
            input=input_messages,
            temperature=temperature,
            max_output_tokens=max_tokens,
            text={"format": {"type": "json_object"}},
        )
        content = (response.output_text or "").strip()
        extracted = _extract_json(content)
        try:
            return json.loads(extracted)
        except json.JSONDecodeError as exc:
            errors.append(str(exc))
            if on_raw_response:
                on_raw_response(content)
            input_messages.append({"role": "assistant", "content": content})
            input_messages.append({"role": "user", "content": retry_prompt})
    raise ValueError(f"Invalid JSON from {model} (responses) after retries: " + " | ".join(errors))


# ---------------------------------------------------------------------------
# Tool-calling ReAct loop
# ---------------------------------------------------------------------------

def call_llm_with_tools(
    client: OpenAI,
    model: str,
    system_prompt: str,
    user_prompt: str,
    tools: list[dict[str, Any]],
    invoke_tool: Callable[[str, str], "ToolInvocation"],
    *,
    max_iterations: int = 25,
    temperature: float = 0.2,
    max_tokens: int = 8192,
    on_step: Optional[Callable[[dict[str, Any]], None]] = None,
    is_finished: Optional[Callable[[str, dict[str, Any]], bool]] = None,
) -> "ToolLoopResult":
    """Run a ReAct-style tool-use loop.

    Parameters
    ----------
    client / model / system_prompt / user_prompt
        Standard LLM identity + the seed conversation.
    tools
        OpenAI-format tool specs (`{type: "function", function: {...}}`).
    invoke_tool
        Callback `(name, args_json) -> ToolInvocation`. The runtime passes the
        result text and any termination flags back to the model.
    max_iterations
        Hard cap on round trips. Each iteration may invoke multiple tools.
    on_step
        Optional observer fired after every assistant turn — gets `{"text": ..., "tool_calls": [...]}`.
    is_finished
        Optional predicate `(name, parsed_args) -> bool` that short-circuits the
        loop when a particular tool is called (e.g. `finish_story`).

    Notes
    -----
    Falls back to chat-completions tool calling for non-Responses models. For
    Responses-API models we still use chat tooling because the Responses API's
    function-calling shape differs and the agents in this project have access
    to chat-capable models (gpt-4o-mini, etc.).
    """
    # Force chat-style tool calling regardless of model API. This keeps the loop
    # uniform across model families and works for every model used in this repo.
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    final_text = ""
    finished = False
    iterations = 0

    # Inject pacing nudges so the agent self-regulates as it nears the cap.
    # Iter > 70% of cap → "wrap up" reminder; final 3 iters → "finish_story now".
    nudge_70 = max(1, int(max_iterations * 0.7))
    nudge_final = max(1, max_iterations - 3)
    nudge_70_emitted = False
    nudge_final_emitted = False

    while iterations < max_iterations and not finished:
        iterations += 1

        # Pacing nudges as system-role reminders. The model sees them inline.
        if iterations == nudge_70 and not nudge_70_emitted:
            messages.append({
                "role": "system",
                "content": (
                    f"PACING: you are at iteration {iterations}/{max_iterations}. "
                    "If validation is not yet passing, focus on the smallest correct fix "
                    "and re-run the validator. Stop exploring — converge."
                ),
            })
            nudge_70_emitted = True
        elif iterations == nudge_final and not nudge_final_emitted:
            messages.append({
                "role": "system",
                "content": (
                    f"FINAL {max_iterations - iterations + 1} ITERATIONS. "
                    "Either run your validator and call finish_story(success=true), "
                    "or call finish_story(success=false) with a clear summary of what is "
                    "blocking you. Do NOT begin new exploration."
                ),
            })
            nudge_final_emitted = True

        try:
            response = client.chat.completions.create(
                model=_chat_fallback_model(model),
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            return ToolLoopResult(
                final_text=f"LLM error: {exc}",
                finished=False,
                iterations=iterations,
                error=str(exc),
            )

        choice = response.choices[0]
        msg = choice.message
        text = (msg.content or "").strip()
        tool_calls = getattr(msg, "tool_calls", None) or []

        if on_step:
            try:
                on_step({
                    "text": text,
                    "tool_calls": [
                        {"name": tc.function.name, "arguments": tc.function.arguments}
                        for tc in tool_calls
                    ],
                    "iteration": iterations,
                })
            except Exception:
                pass

        # Add assistant turn to history
        if tool_calls:
            messages.append({
                "role": "assistant",
                "content": text or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            })

            # Invoke each tool sequentially
            for tc in tool_calls:
                tool_name = tc.function.name
                arg_text = tc.function.arguments or "{}"
                invocation = invoke_tool(tool_name, arg_text)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tool_name,
                    "content": invocation.content_for_model[:8000],
                })
                if is_finished:
                    try:
                        parsed = json.loads(arg_text) if arg_text else {}
                    except json.JSONDecodeError:
                        parsed = {}
                    if is_finished(tool_name, parsed):
                        final_text = text
                        finished = True
                        break
                if invocation.terminate:
                    final_text = text or invocation.content_for_model
                    finished = True
                    break
        else:
            messages.append({"role": "assistant", "content": text})
            final_text = text
            finished = True
            break

    return ToolLoopResult(
        final_text=final_text,
        finished=finished,
        iterations=iterations,
        messages=messages,
    )


def _chat_fallback_model(model: str) -> str:
    """If the configured model is a Responses-API model with no chat function-calling support,
    fall back to gpt-4o-mini for the tool loop. Modern chat models pass through unchanged."""
    if not model:
        return "gpt-4o-mini"
    if is_responses_model(model):
        return "gpt-4o-mini"
    if is_completions_model(model):
        return "gpt-4o-mini"
    return model


class ToolInvocation:
    """Lightweight data carrier passed back from the host to the loop."""

    __slots__ = ("content_for_model", "terminate")

    def __init__(self, content_for_model: str, terminate: bool = False):
        self.content_for_model = content_for_model
        self.terminate = terminate


class ToolLoopResult:
    """Outcome of a tool-using loop."""

    __slots__ = ("final_text", "finished", "iterations", "messages", "error")

    def __init__(
        self,
        final_text: str,
        finished: bool,
        iterations: int,
        messages: Optional[list[dict[str, Any]]] = None,
        error: Optional[str] = None,
    ):
        self.final_text = final_text
        self.finished = finished
        self.iterations = iterations
        self.messages = messages or []
        self.error = error
