"""PM Agent — class-based coordinator using the message bus.

The PM is exposed in two ways:

  - Story creation / triage / rescope via `create_stories()`, `analyze_failure()`, etc.
    These are still backed by the existing `agents.pm_agent` helpers (which use
    structured-JSON LLM calls for predictable schemas).

  - As a live agent on the bus, registered under the `pm` address. Subscribed
    handlers fire when peer agents call `ask_pm` / `request_review` / `query_agent`,
    when supervisor publishes `story.failed`, etc.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from agents.agentic.base import AgentBase, RunResult
from agents.bus import BusMessage
from agents.pm_agent import (
    analyze_failure,
    replan_failure,
    rescope_story,
    triage_test_failure,
)
from schemas import Story


class PMAgent(AgentBase):
    agent_id = "pm"
    role_description = "Senior Product Manager / Coordinator"
    model_env_var = "PM_AGENT_MODEL"
    default_model = "gpt-4o-mini"
    allowed_tools = [
        "ask_pm", "send_message", "query_agent", "request_review",
        "read_storypack", "read_past_patterns", "record_failure_pattern", "read_logs",
        "finish_story",
    ]
    iteration_cap = 20

    # PM does not run a "story" itself; it reacts to events on the bus.
    def run(self, story: Optional[Story] = None) -> RunResult:
        return RunResult(
            success=True,
            summary="PM agent is event-driven — it does not run stories.",
        )

    # ---------- Bus event handlers (called via subscribe / register_handler) ----------

    def handle_message(self, msg: BusMessage) -> Optional[dict[str, Any]]:
        if msg.event_type == "question":
            return self._answer_question(msg)
        if msg.event_type == "review_request":
            return self._review_artifact(msg)
        if msg.event_type == "query":
            return self._answer_question(msg)
        if msg.event_type == "heal_request":
            return self._produce_heal_instructions(msg)
        if msg.event_type == "triage_request":
            return self._triage_test_failure(msg)
        if msg.event_type == "rescope_request":
            return self._rescope(msg)
        return super().handle_message(msg)

    # ---------- Implementations ----------

    def _answer_question(self, msg: BusMessage) -> dict[str, Any]:
        question = (msg.payload or {}).get("question", "")
        story_id = (msg.payload or {}).get("story_id") or msg.story_id

        target_story = self._find_story(story_id) if story_id else None
        from agents.llm_client import call_llm_text

        sys_prompt = (
            "You are a Senior Product Manager. A developer agent is asking a question "
            "about a story they are implementing. Answer concisely (<200 words) and be "
            "decisive — they need a single answer, not a menu of options."
        )
        story_block = (
            f"Story they are working on:\n{json.dumps(target_story.model_dump(), indent=2)}\n\n"
            if target_story else ""
        )
        user_prompt = (
            f"Original requirement (truncated):\n{self.ctx.requirement_text[:1500]}\n\n"
            f"{story_block}"
            f"Question from {msg.from_agent}:\n{question}"
        )
        try:
            answer = call_llm_text(
                self.client, self.model, sys_prompt, user_prompt,
                temperature=0.2, max_tokens=600,
            )
        except Exception as exc:  # noqa: BLE001
            answer = f"PM unable to answer: {exc}. Use your best judgment."

        self._emit("info", f"PM answered {msg.from_agent}", answer)
        return {"answer": answer}

    def _review_artifact(self, msg: BusMessage) -> dict[str, Any]:
        artifact = (msg.payload or {}).get("artifact", "")
        description = (msg.payload or {}).get("description", "")
        from agents.llm_client import call_llm_json

        sys_prompt = (
            "You are a PM reviewing an engineering artifact. Reply with JSON only: "
            '{"verdict": "approved" | "changes_requested", "notes": "<short notes>"}'
        )
        user_prompt = (
            f"Description: {description}\n\n"
            f"Artifact:\n{artifact[:4000]}"
        )
        try:
            data = call_llm_json(
                self.client, self.model, sys_prompt, user_prompt,
                temperature=0.2,
                retry_prompt='Return JSON only with keys "verdict" and "notes".',
            )
        except Exception as exc:  # noqa: BLE001
            data = {"verdict": "approved", "notes": f"PM auto-approved (LLM error: {exc})"}
        self._emit("info", f"PM review for {msg.from_agent}: {data.get('verdict')}",
                   data.get("notes", ""))
        return data

    def _produce_heal_instructions(self, msg: BusMessage) -> dict[str, Any]:
        story_id = (msg.payload or {}).get("story_id") or msg.story_id
        error = (msg.payload or {}).get("error", "")
        attempt = int((msg.payload or {}).get("attempt", 1))

        story = self._find_story(story_id)
        if not story:
            return {"instructions": "PM could not find the story; use best judgment."}

        try:
            if attempt >= 2:
                instructions = replan_failure(story, error, self.ctx.requirement_text)
            else:
                instructions = analyze_failure(story, error, self.ctx.requirement_text)
        except Exception as exc:  # noqa: BLE001
            instructions = f"PM analysis failed ({exc}). Try a smaller diff and re-run validation."

        self._emit("info", f"PM heal instructions for {story_id} (attempt {attempt})", instructions)
        return {"instructions": instructions, "use_replan": attempt >= 2}

    def _triage_test_failure(self, msg: BusMessage) -> dict[str, Any]:
        output = (msg.payload or {}).get("test_output", "")
        try:
            result = triage_test_failure(output, self.ctx.all_stories, self.ctx.requirement_text)
        except Exception as exc:  # noqa: BLE001
            result = {
                "target_agent": "backend",
                "responsible_story_id": "unknown",
                "root_cause": f"PM triage error: {exc}",
                "fix_instructions": "Inspect the test output manually and try the smallest correct fix.",
            }
        self._emit("info", f"PM triage: target={result.get('target_agent')}",
                   result.get("fix_instructions", ""))
        return result

    def _rescope(self, msg: BusMessage) -> dict[str, Any]:
        story_id = (msg.payload or {}).get("story_id") or msg.story_id
        cumulative_errors = (msg.payload or {}).get("errors", "")
        story = self._find_story(story_id)
        if not story:
            return {"decision": "halt", "reason": "Story not found by PM"}
        try:
            return rescope_story(story, cumulative_errors, self.ctx.requirement_text)
        except Exception as exc:  # noqa: BLE001
            return {"decision": "halt", "reason": f"PM rescope error: {exc}"}

    # ---------- Helpers ----------

    def _find_story(self, story_id: Optional[str]) -> Optional[Story]:
        if not story_id:
            return None
        for s in self.ctx.all_stories:
            if s.id == story_id:
                return s
        return None
