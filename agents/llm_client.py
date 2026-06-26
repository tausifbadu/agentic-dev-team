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
import os
import re
import time
from types import SimpleNamespace
from typing import Any, Callable, Optional

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    BadRequestError,
    OpenAI,
)

# Hard per-call timeout. A plain float timeout did NOT bound a stalled read once
# (a gateway call hung ~79 min and froze the whole run). An explicit httpx.Timeout
# bounds connect/read/write/pool so a hung connection fails fast and our backoff
# retries instead. SDK retries are disabled here — backoff lives in
# _chat_completions_create so the two don't compound.
_LLM_TIMEOUT = float(os.getenv("AGENTIC_LLM_TIMEOUT", "180"))

# Stream chat completions. The httpx read timeout (_LLM_TIMEOUT) bounds a single
# read; for a non-streamed call that read is the WHOLE response, so a slow
# reasoning generation (e.g. the PM storypack, up to 16k tokens) trips the timeout
# even when the gateway is healthy. Streaming makes the read timeout bound the gap
# BETWEEN chunks instead — a long generation never trips it as long as tokens keep
# flowing. Set AGENTIC_LLM_STREAM=0 to fall back to blocking calls if a gateway
# misbehaves with SSE.
_LLM_STREAM = os.getenv("AGENTIC_LLM_STREAM", "1") not in ("0", "false", "False", "")


def make_openai_client(timeout: Optional[float] = None) -> OpenAI:
    """Build an OpenAI client with a bounded timeout and no SDK-level retries."""
    t = timeout if timeout is not None else _LLM_TIMEOUT
    return OpenAI(
        timeout=httpx.Timeout(t, connect=15.0),
        max_retries=0,
    )

RESPONSES_MODEL_PATTERNS = ("codex",)
LEGACY_COMPLETIONS_MODEL_PATTERNS = ("davinci", "babbage", "cushman")

# Backoff for rate limits (429) / transient transport errors. The shared LLM
# gateway throttles aggressively under a multi-story run; without this every
# agent call crashes on the first 429. Tunable via env.
_LLM_MAX_RETRIES = int(os.getenv("AGENTIC_LLM_MAX_RETRIES", "6"))
_LLM_BACKOFF_BASE = float(os.getenv("AGENTIC_LLM_BACKOFF_BASE", "4"))
_LLM_BACKOFF_MAX = float(os.getenv("AGENTIC_LLM_BACKOFF_MAX", "45"))


# ---------------------------------------------------------------------------
# Token-usage telemetry
# ---------------------------------------------------------------------------
# Process-global accumulator. The runtime is single-run and sequential, so a
# module global is enough: the supervisor snapshots deltas around each story to
# attribute tokens per story, and reads the totals at run end. `cached_tokens`
# surfaces how much of the prompt the gateway served from cache — the key signal
# for whether our prefix is stable enough to benefit from prompt caching.
_USAGE = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}


def record_usage(usage: Any) -> None:
    """Accumulate token counts from a response `usage` object.

    Handles both the chat/completions shape (`prompt_tokens` / `completion_tokens`
    / `prompt_tokens_details.cached_tokens`) and the Responses-API shape
    (`input_tokens` / `output_tokens` / `input_tokens_details.cached_tokens`).
    Best-effort: never raises into the call path.
    """
    if usage is None:
        return
    try:
        pt = int(getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0) or 0)
        ct = int(getattr(usage, "completion_tokens", 0) or getattr(usage, "output_tokens", 0) or 0)
        cached = 0
        for attr in ("prompt_tokens_details", "input_tokens_details"):
            details = getattr(usage, attr, None)
            if details is not None:
                cached = int(getattr(details, "cached_tokens", 0) or 0)
                break
        _USAGE["calls"] += 1
        _USAGE["prompt_tokens"] += pt
        _USAGE["completion_tokens"] += ct
        _USAGE["cached_tokens"] += cached
    except Exception:  # noqa: BLE001
        pass


def usage_snapshot() -> dict:
    """Current cumulative totals (copy)."""
    return dict(_USAGE)


def usage_delta(before: dict) -> dict:
    """Counts accumulated since the `before` snapshot, plus a cache_hit_pct."""
    d = {k: _USAGE.get(k, 0) - int(before.get(k, 0)) for k in _USAGE}
    pt = d.get("prompt_tokens", 0)
    d["total_tokens"] = pt + d.get("completion_tokens", 0)
    d["cache_hit_pct"] = round(100 * d.get("cached_tokens", 0) / pt, 1) if pt else 0.0
    return d


def reset_usage() -> None:
    for k in _USAGE:
        _USAGE[k] = 0


# Per-tool-result size fed back to the model. Sits on top of each tool's own
# trimming. Middle-out (head + larger tail) rather than a head cut, because
# errors/tracebacks live at the END of command output — head-truncating them
# leaves the agent unable to diagnose failures, so it loops and burns iterations.
_TOOL_RESULT_MAX_CHARS = int(os.getenv("AGENTIC_TOOL_RESULT_MAX_CHARS", "24000"))

# How many times the ReAct loop will nudge a model that ended its turn with a
# plain-text message (no tool call) and no validated finish_story. Weaker/cheaper
# models sometimes NARRATE completion ("Implemented X...") instead of emitting the
# required finish_story call; without this they fail spuriously. Bounded so a model
# that keeps narrating still ends gracefully.
_MAX_NO_FINISH_NUDGES = int(os.getenv("AGENTIC_MAX_NO_FINISH_NUDGES", "2"))


def _clip_tool_result(text: str, limit: int = _TOOL_RESULT_MAX_CHARS) -> str:
    if not text or len(text) <= limit:
        return text
    head = limit // 3
    tail = limit - head
    elided = len(text) - head - tail
    return (
        text[:head]
        + f"\n\n... [{elided} chars elided from the middle — "
        "read_file offset/limit or run_python to inspect more] ...\n\n"
        + text[-tail:]
    )


def _retry_after_seconds(exc: Exception) -> Optional[float]:
    """Honor a Retry-After header from the gateway when present."""
    try:
        ra = exc.response.headers.get("retry-after")  # type: ignore[attr-defined]
        if ra:
            return float(ra)
    except Exception:
        pass
    return None


def _backoff_delay(attempt: int, exc: Exception) -> float:
    """Retry-After if given, else exponential backoff capped at _LLM_BACKOFF_MAX."""
    return _retry_after_seconds(exc) or min(
        _LLM_BACKOFF_BASE * (2 ** (attempt - 1)), _LLM_BACKOFF_MAX
    )


def _model_api(model_name: str) -> str:
    """Return 'responses', 'completions', or 'chat' based on the model name."""
    lower = model_name.lower()
    # Gateway/namespaced model ids ("provider/model", e.g. "codex/gpt-5.5") are
    # served over the standard OpenAI chat-completions API. Treat them as chat so
    # the "codex" heuristic below doesn't misroute them to the Responses API or
    # trigger the chat-fallback model in _chat_fallback_model.
    if "/" in lower:
        return "chat"
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


def _temperature_unsupported_error(err: str) -> bool:
    e = err.lower()
    return "temperature" in e and (
        "unsupported" in e or "only the default" in e or "default (1)" in e
    )


def _consume_stream(stream: Any):
    """Drain a streamed chat completion into a non-streamed response shape.

    Reconstructs ``choices[0].message.content``, ``.tool_calls``,
    ``choices[0].finish_reason`` and ``.usage`` so every caller can treat the
    result exactly like a blocking ``chat.completions.create`` response. The
    iteration here is where per-chunk reads happen, so a stalled gateway raises
    ``APITimeoutError`` mid-stream and the caller's backoff handles it.

    Returns an object whose ``choices`` is empty when the stream carried no
    content, tool calls, or finish_reason (the gateway's "exhausted" sibling of
    an empty-choices non-streamed response) so the existing retry path triggers.
    """
    content_parts: list[str] = []
    tool_calls_acc: dict[int, dict] = {}
    finish_reason: Optional[str] = None
    usage = None
    role = "assistant"

    for chunk in stream:
        if getattr(chunk, "usage", None):
            usage = chunk.usage
        for choice in (getattr(chunk, "choices", None) or []):
            if getattr(choice, "finish_reason", None):
                finish_reason = choice.finish_reason
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue
            if getattr(delta, "role", None):
                role = delta.role
            if getattr(delta, "content", None):
                content_parts.append(delta.content)
            for tc in (getattr(delta, "tool_calls", None) or []):
                idx = getattr(tc, "index", 0) or 0
                slot = tool_calls_acc.setdefault(
                    idx, {"id": None, "type": "function", "name": "", "args": ""}
                )
                if getattr(tc, "id", None):
                    slot["id"] = tc.id
                if getattr(tc, "type", None):
                    slot["type"] = tc.type
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        slot["name"] += fn.name
                    if getattr(fn, "arguments", None):
                        slot["args"] += fn.arguments

    tool_calls = [
        SimpleNamespace(
            id=slot["id"],
            type=slot["type"] or "function",
            function=SimpleNamespace(name=slot["name"], arguments=slot["args"]),
        )
        for _, slot in sorted(tool_calls_acc.items())
    ]
    content = "".join(content_parts)

    # Nothing meaningful came back — surface as empty choices so the caller's
    # transient-retry path kicks in rather than returning a hollow message.
    if not content and not tool_calls and finish_reason is None:
        return SimpleNamespace(choices=[], usage=usage)

    message = SimpleNamespace(
        role=role,
        content=content or None,
        tool_calls=tool_calls or None,
    )
    choice = SimpleNamespace(index=0, message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage)


def _chat_completions_create(
    client: OpenAI,
    *,
    model: str,
    max_tokens: int,
    **kwargs: Any,
):
    """Call ``chat.completions.create``. Retries when the model rejects ``max_tokens``
    (use ``max_completion_tokens``) or custom ``temperature`` (omit param; API default only).
    """
    use_mc = False
    omit_temp = False
    stream = _LLM_STREAM
    omit_stream_opts = False
    rl_attempts = 0
    while True:
        call_kw = {k: v for k, v in kwargs.items() if not (omit_temp and k == "temperature")}
        token_kw = (
            {"max_completion_tokens": max_tokens} if use_mc else {"max_tokens": max_tokens}
        )
        if stream:
            call_kw["stream"] = True
            if not omit_stream_opts:
                # Ask the gateway to emit a final usage chunk so token telemetry
                # survives streaming.
                call_kw["stream_options"] = {"include_usage": True}
        try:
            raw = client.chat.completions.create(model=model, **token_kw, **call_kw)
            # Iterating the stream is where per-chunk reads (and thus read
            # timeouts) happen, so keep it inside the try for the retry handlers.
            resp = _consume_stream(raw) if stream else raw
        except BadRequestError as exc:
            err = str(exc).lower()
            if not use_mc and (
                "max_completion_tokens" in err
                or ("max_tokens" in err and "unsupported" in err)
            ):
                use_mc = True
                continue
            if not omit_temp and _temperature_unsupported_error(str(exc)):
                omit_temp = True
                continue
            # Some gateways reject stream_options (or streaming itself) with a 400.
            # Degrade gracefully: drop the option first, then disable streaming.
            if stream and not omit_stream_opts and "stream_options" in err:
                omit_stream_opts = True
                continue
            if stream and "stream" in err:
                stream = False
                continue
            raise
        except APIStatusError as exc:
            # Retry rate limits (429) and transient gateway/server errors (5xx —
            # includes the gateway's "provider exhausted" 502s and HTML error pages).
            # BadRequestError (400) is handled above and re-raised, not retried.
            status = getattr(exc, "status_code", 0) or 0
            if (status == 429 or status >= 500) and rl_attempts < _LLM_MAX_RETRIES:
                rl_attempts += 1
                time.sleep(_backoff_delay(rl_attempts, exc))
                continue
            raise
        except (APIConnectionError, APITimeoutError) as exc:
            if rl_attempts >= _LLM_MAX_RETRIES:
                raise
            rl_attempts += 1
            time.sleep(_backoff_delay(rl_attempts, exc))
            continue

        # Some gateways return HTTP 200 with an empty `choices` list when the
        # upstream provider is exhausted/throttled (sibling of the 502 "provider
        # exhausted" path above). Every caller immediately indexes choices[0], so an
        # empty list raises IndexError — in the ReAct loop that crashes the whole
        # story, in call_llm_json it crashes PM story generation. Treat it as the
        # transient condition it is: back off and retry on the shared retry budget,
        # then surface a clear error rather than a bare IndexError.
        if not getattr(resp, "choices", None):
            if rl_attempts < _LLM_MAX_RETRIES:
                rl_attempts += 1
                time.sleep(_backoff_delay(rl_attempts, Exception("empty choices")))
                continue
            raise RuntimeError(
                f"LLM gateway returned a response with no choices after "
                f"{_LLM_MAX_RETRIES} retries (model={model})."
            )
        record_usage(getattr(resp, "usage", None))
        return resp


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
        try:
            response = client.responses.create(
                model=model,
                instructions=system_prompt,
                input=user_prompt,
                temperature=temperature,
                max_output_tokens=max_tokens,
            )
        except BadRequestError as exc:
            if _temperature_unsupported_error(str(exc)):
                response = client.responses.create(
                    model=model,
                    instructions=system_prompt,
                    input=user_prompt,
                    max_output_tokens=max_tokens,
                )
            else:
                raise
        record_usage(getattr(response, "usage", None))
        return (response.output_text or "").strip()

    if api == "completions":
        try:
            response = client.completions.create(
                model=model,
                prompt=f"{system_prompt}\n\n{user_prompt}",
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except BadRequestError as exc:
            if _temperature_unsupported_error(str(exc)):
                response = client.completions.create(
                    model=model,
                    prompt=f"{system_prompt}\n\n{user_prompt}",
                    max_tokens=max_tokens,
                )
            else:
                raise
        record_usage(getattr(response, "usage", None))
        return (response.choices[0].text or "").strip()

    response = _chat_completions_create(
        client,
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=temperature,
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
        response = _chat_completions_create(
            client,
            model=model,
            max_tokens=max_tokens,
            messages=messages,
            temperature=temperature,
            response_format={"type": "json_object"},
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
        try:
            response = client.completions.create(
                model=model,
                prompt=prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except BadRequestError as exc:
            if not _temperature_unsupported_error(str(exc)):
                raise
            response = client.completions.create(
                model=model,
                prompt=prompt,
                max_tokens=max_tokens,
            )
        record_usage(getattr(response, "usage", None))
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
        try:
            response = client.responses.create(
                model=model,
                instructions=system_prompt,
                input=input_messages,
                temperature=temperature,
                max_output_tokens=max_tokens,
                text={"format": {"type": "json_object"}},
            )
        except BadRequestError as exc:
            if not _temperature_unsupported_error(str(exc)):
                raise
            response = client.responses.create(
                model=model,
                instructions=system_prompt,
                input=input_messages,
                max_output_tokens=max_tokens,
                text={"format": {"type": "json_object"}},
            )
        record_usage(getattr(response, "usage", None))
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

# Working-memory compaction.
#
# The ReAct loop accumulates every assistant turn + every tool result and resends
# the whole transcript on each iteration, so token cost (and per-call latency)
# grows ~quadratically over a long story. On a wall-clock-bounded run this is what
# starves later stories of time. Compaction folds the OLDEST rounds into a single
# rolling-summary system message, keeping the head (system + task), that summary,
# and the most recent rounds verbatim.
#
# Hard constraint honored below: the chat API requires every assistant message
# that carries `tool_calls` to be immediately followed by a `tool` message for
# each call id. We therefore compact whole *rounds* (an assistant turn plus its
# tool replies), never individual messages, so pairing is never broken.

_COMPACT_MARKER = "[COMPACTED PROGRESS — earlier steps summarized below]"
_COMPACT_SUMMARY_MAX = 12000  # cap the rolling summary so it can't itself bloat


def _estimate_chars(messages: list[dict[str, Any]]) -> int:
    """Coarse size estimate of the running transcript (chars ≈ 4× tokens)."""
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            total += len(content)
        for tc in (m.get("tool_calls") or []):
            total += len((tc.get("function") or {}).get("arguments") or "")
    return total


def _group_rounds(body: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group body messages into rounds. A round begins at each assistant message;
    non-assistant messages (tool replies, injected system nudges) attach to the
    current round. This keeps every assistant `tool_calls` turn together with its
    matching `tool` replies so compaction can drop whole rounds safely.
    """
    rounds: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    for m in body:
        if m.get("role") == "assistant" and cur:
            rounds.append(cur)
            cur = [m]
        else:
            cur.append(m)
    if cur:
        rounds.append(cur)
    return rounds


def _summarize_round(round_msgs: list[dict[str, Any]]) -> str:
    """Render one dropped round as a few compact lines: what the agent did and
    whether each tool succeeded. Purely mechanical — no extra LLM call."""
    lines: list[str] = []
    for m in round_msgs:
        role = m.get("role")
        if role == "assistant":
            tcs = m.get("tool_calls") or []
            if tcs:
                names = ", ".join((tc.get("function") or {}).get("name", "?") for tc in tcs)
                lines.append(f"- called: {names}")
            txt = (m.get("content") or "").strip()
            if txt:
                lines.append(f"  thought: {txt[:140]}")
        elif role == "tool":
            name = m.get("name", "tool")
            content = (m.get("content") or "").strip().replace("\n", " ")
            status = "ERROR" if content.startswith("ERROR") else "ok"
            lines.append(f"    {name} -> {status}: {content[:160]}")
        # injected system pacing nudges are transient — omit from the summary.
    return "\n".join(lines)


def _compact_messages(
    messages: list[dict[str, Any]], keep_last_rounds: int
) -> list[dict[str, Any]]:
    """Fold all but the last `keep_last_rounds` rounds into one rolling-summary
    system message. Returns the original list unchanged if there is nothing
    meaningful to compact.
    """
    if len(messages) < 3:
        return messages
    head = messages[:2]  # system prompt + user task — always verbatim

    prior_summary = ""
    body: list[dict[str, Any]] = []
    for m in messages[2:]:
        if (
            m.get("role") == "system"
            and isinstance(m.get("content"), str)
            and m["content"].startswith(_COMPACT_MARKER)
        ):
            prior_summary = m["content"]
            continue
        body.append(m)

    rounds = _group_rounds(body)
    if len(rounds) <= keep_last_rounds:
        return messages

    drop = rounds[: -keep_last_rounds]
    keep = rounds[-keep_last_rounds:]

    new_chunk = "\n".join(s for s in (_summarize_round(r) for r in drop) if s.strip())
    summary_body = (prior_summary or _COMPACT_MARKER) + "\n" + new_chunk
    if len(summary_body) > _COMPACT_SUMMARY_MAX:
        # Keep the marker + the most recent tail of the summary.
        tail = summary_body[-_COMPACT_SUMMARY_MAX:]
        summary_body = f"{_COMPACT_MARKER}\n... (older steps elided) ...\n{tail}"

    summary_msg = {"role": "system", "content": summary_body}
    flat_keep = [m for r in keep for m in r]
    return head + [summary_msg] + flat_keep


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
    compact_at_chars: Optional[int] = None,
    keep_last_rounds: int = 6,
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
    compact_at_chars
        When set and the running transcript exceeds this many characters, the
        oldest rounds are folded into a rolling summary so context stays bounded
        (see `_compact_messages`). `None` disables compaction.
    keep_last_rounds
        How many of the most recent rounds to keep verbatim during compaction.

    Notes
    -----
    Falls back to chat-completions tool calling for non-Responses models. For
    Responses-API models we still use chat tooling because the Responses API's
    function-calling shape differs and the agents in this project have access
    to chat-capable models (codex/gpt-5.5, etc.).
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
    compactions = 0

    # Inject pacing nudges so the agent self-regulates as it nears the cap.
    # Iter > 70% of cap → "wrap up" reminder; final 3 iters → "finish_story now".
    nudge_70 = max(1, int(max_iterations * 0.7))
    nudge_final = max(1, max_iterations - 3)
    nudge_70_emitted = False
    nudge_final_emitted = False
    no_finish_nudges = 0

    while iterations < max_iterations and not finished:
        iterations += 1

        # Compact the transcript at a safe point (between fully-completed rounds)
        # before building the next request, so context stays bounded.
        if compact_at_chars and _estimate_chars(messages) > compact_at_chars:
            compacted = _compact_messages(messages, keep_last_rounds)
            if len(compacted) < len(messages):
                messages = compacted
                compactions += 1

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
            response = _chat_completions_create(
                client,
                model=_chat_fallback_model(model),
                max_tokens=max_tokens,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=temperature,
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
                    "content": _clip_tool_result(invocation.content_for_model),
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
            # Plain-text turn, no tool call. Weaker models sometimes NARRATE
            # completion ("Implemented X...") instead of calling finish_story, which
            # would otherwise end the loop as a failure. Nudge it (bounded) to emit
            # the proper tool call before accepting the plain message as the end.
            messages.append({"role": "assistant", "content": text})
            if no_finish_nudges < _MAX_NO_FINISH_NUDGES:
                no_finish_nudges += 1
                messages.append({
                    "role": "system",
                    "content": (
                        "You ended your turn with a plain text message and called NO tool. "
                        "Prose does not complete the story. If the work is done and your "
                        "validator passes, call finish_story(success=true). If you are blocked, "
                        "call finish_story(success=false) with a clear summary. Otherwise keep "
                        "working with the appropriate tool calls — do not reply in prose."
                    ),
                })
                continue
            final_text = text
            finished = True
            break

    return ToolLoopResult(
        final_text=final_text,
        finished=finished,
        iterations=iterations,
        messages=messages,
        compactions=compactions,
    )


# Model used to drive the chat-style tool loop when the configured model can't do
# it directly (Responses-API and legacy-completions models don't support the
# chat-completions function-calling shape the ReAct loop relies on). Env-tunable;
# defaults to the gateway's gpt-5.5. Chat models pass through untouched.
_CHAT_FALLBACK_MODEL = os.getenv("AGENTIC_CHAT_FALLBACK_MODEL", "codex/gpt-5.5")


def _chat_fallback_model(model: str) -> str:
    """If the configured model can't drive the chat-style tool loop (a Responses-API
    or legacy-completions model), fall back to _CHAT_FALLBACK_MODEL (default
    codex/gpt-5.5). Modern chat models pass through unchanged."""
    if not model:
        return _CHAT_FALLBACK_MODEL
    if is_responses_model(model):
        return _CHAT_FALLBACK_MODEL
    if is_completions_model(model):
        return _CHAT_FALLBACK_MODEL
    return model


class ToolInvocation:
    """Lightweight data carrier passed back from the host to the loop."""

    __slots__ = ("content_for_model", "terminate")

    def __init__(self, content_for_model: str, terminate: bool = False):
        self.content_for_model = content_for_model
        self.terminate = terminate


class ToolLoopResult:
    """Outcome of a tool-using loop."""

    __slots__ = ("final_text", "finished", "iterations", "messages", "error", "compactions")

    def __init__(
        self,
        final_text: str,
        finished: bool,
        iterations: int,
        messages: Optional[list[dict[str, Any]]] = None,
        error: Optional[str] = None,
        compactions: int = 0,
    ):
        self.final_text = final_text
        self.finished = finished
        self.iterations = iterations
        self.messages = messages or []
        self.error = error
        self.compactions = compactions
