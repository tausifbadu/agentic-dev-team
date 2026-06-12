"""PM Agent. Produces implementation-ready stories with stronger structure."""

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from openai import OpenAI
from agents.llm_client import make_openai_client

from schemas import Requirement, Story, StoryPack

MODEL_NAME = os.getenv("PM_AGENT_MODEL", "gpt-4o-mini")

SYSTEM_PROMPT = """You are a Product Manager. Given a requirement, produce implementation-ready user stories for engineering agents.

Output JSON only. No markdown, no explanation. Format:
{
  "stories": [
    {
      "id": "story_1",
      "title": "Short title",
      "description": "One sentence",
      "acceptance_criteria": ["AC1", "AC2"],
      "implementation_notes": ["Important design note"],
      "test_focus": ["What must be tested"],
      "ownership": "frontend" or "backend" or "testing",
      "dependencies": ["story_1"]
    }
  ]
}

Rules:
- Each story has one ownership: frontend, backend, or testing
- Acceptance criteria must be observable and testable
- Keep stories small and focused
- Add implementation_notes that tell coding agents what matters technically
- Add test_focus that tells coding agents how to validate the story
- Split mixed concerns into separate frontend/backend stories when useful
- Use ids: story_1, story_2, ...
- Add dependencies as a list of story ids that must complete before this story starts

Story ordering and dependency guidance:
- Order backend API contract stories first so frontend stories can reference them
- When frontend depends on backend, include a backend story that clearly defines the API contract and response schema
- Group related stories: backend API contract -> backend logic -> backend error handling -> frontend data/input -> frontend visualization -> frontend states -> testing
- If a chart or visualization is required, clearly define the backend response fields the frontend will consume

Quality standards:
- Identify edge cases early and include them as acceptance criteria
- Prefer free and practical libraries unless otherwise required
- Build for localhost development first
- Do not mix unrelated concerns in one story
- Testing stories should cover API tests, UI tests, and integration tests"""

JSON_RETRY_PROMPT = """Your previous response was not valid JSON. Return ONLY valid JSON matching this schema:
{
  "stories": [
    {
      "id": "story_1",
      "title": "Short title",
      "description": "One sentence",
      "acceptance_criteria": ["AC1", "AC2"],
      "implementation_notes": ["Important design note"],
      "test_focus": ["What must be tested"],
      "ownership": "frontend" or "backend" or "testing",
      "dependencies": []
    }
  ]
}
No markdown fences. No commentary."""

MAX_JSON_PARSE_RETRIES = 2
DEBUG_DIR = Path(__file__).parent.parent / "workspace" / "debug"


def _store_raw_response(raw: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    out = DEBUG_DIR / f"pm_raw_{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}.txt"
    out.write_text(raw, encoding="utf-8")


def _call_pm_llm(client: OpenAI, requirement_text: str) -> dict:
    from agents.llm_client import call_llm_json
    return call_llm_json(
        client, MODEL_NAME, SYSTEM_PROMPT, requirement_text,
        temperature=0.3,
        max_retries=MAX_JSON_PARSE_RETRIES,
        retry_prompt=JSON_RETRY_PROMPT,
        on_raw_response=_store_raw_response,
    )


ERROR_ANALYSIS_PROMPT = """You are a Product Manager triaging a developer agent failure.

A developer agent attempted to implement a story but failed. You will receive:
- The story details (title, description, acceptance criteria)
- The error output from the failed attempt
- The original requirement for context

Your job:
1. Classify the error (missing dependency, syntax error, wrong API usage, file structure issue, etc.)
2. Identify the root cause in 1-2 sentences
3. Produce concrete, actionable fix instructions that the developer agent can follow to resolve the issue
4. Keep your response under 500 words
5. Do NOT produce code. Give clear instructions only.

Respond in plain text. No JSON. No markdown fences."""


def analyze_failure(story: Story, error_output: str, requirement_text: str) -> str:
    """PM agent analyzes a developer failure and returns fix instructions."""
    from agents.llm_client import call_llm_text

    client = make_openai_client()
    user_prompt = f"""Story that failed:
  Title: {story.title}
  Description: {story.description}
  Ownership: {story.ownership}
  Acceptance Criteria: {json.dumps(story.acceptance_criteria)}
  Implementation Notes: {json.dumps(story.implementation_notes)}

Error output from the developer agent:
{error_output[:3000]}

Original requirement (truncated):
{requirement_text[:1500]}

Analyze the failure and provide fix instructions for the developer agent."""

    return call_llm_text(
        client, MODEL_NAME, ERROR_ANALYSIS_PROMPT, user_prompt,
        temperature=0.3, max_tokens=2048,
    )


TEST_TRIAGE_PROMPT = """You are a Product Manager triaging a test failure.

A test suite ran against the backend API and frontend app and produced failures.
You will receive:
- The full test output (stdout + stderr)
- All stories in the storypack (for context on what was built)
- The original requirement

Your job:
1. Classify the root cause: is the bug in **backend** code, **frontend** code, or the **test** itself?
2. Identify which story (by id) is most likely responsible, or "unknown" if unclear.
3. Produce concrete, actionable fix instructions for the responsible agent.
4. Keep your response under 500 words.

Respond with JSON only:
{
  "target_agent": "backend" | "frontend" | "testing",
  "responsible_story_id": "story_id or unknown",
  "root_cause": "1-2 sentence explanation",
  "fix_instructions": "detailed instructions for the agent"
}"""

REPLAN_PROMPT = """You are a Product Manager re-planning a failed story.

A developer agent has failed to implement a story after multiple attempts. The simple
fix-and-retry approach is not working. You must produce a fundamentally revised
implementation plan that takes a different approach.

You will receive:
- The story details
- The cumulative error history from all attempts
- The original requirement

Produce a revised plan that:
1. Identifies why the previous approach failed
2. Proposes an alternative implementation strategy
3. Lists specific files to create/modify
4. Includes concrete code-level guidance

Respond in plain text. No JSON. No markdown fences. Keep under 800 words."""


def triage_test_failure(
    test_output: str, stories: list[Story], requirement_text: str
) -> dict:
    """PM triages test failure: identifies target agent, root cause, and fix instructions."""
    from agents.llm_client import call_llm_json

    client = make_openai_client()
    stories_summary = json.dumps(
        [{"id": s.id, "title": s.title, "ownership": s.ownership,
          "acceptance_criteria": s.acceptance_criteria}
         for s in stories],
        indent=2,
    )
    user_prompt = f"""Test output:
{test_output[:4000]}

All stories:
{stories_summary}

Original requirement (truncated):
{requirement_text[:1500]}

Triage this test failure and return JSON."""

    return call_llm_json(
        client, MODEL_NAME, TEST_TRIAGE_PROMPT, user_prompt,
        temperature=0.3,
        max_retries=MAX_JSON_PARSE_RETRIES,
        retry_prompt=JSON_RETRY_PROMPT,
        on_raw_response=_store_raw_response,
    )


def replan_failure(
    story: Story, cumulative_errors: str, requirement_text: str
) -> str:
    """PM produces a revised implementation plan after repeated failures."""
    from agents.llm_client import call_llm_text

    client = make_openai_client()
    user_prompt = f"""Story that keeps failing:
  Title: {story.title}
  Description: {story.description}
  Ownership: {story.ownership}
  Acceptance Criteria: {json.dumps(story.acceptance_criteria)}
  Implementation Notes: {json.dumps(story.implementation_notes)}

Cumulative error history from all attempts:
{cumulative_errors[:5000]}

Original requirement (truncated):
{requirement_text[:1500]}

Produce a completely revised implementation plan."""

    return call_llm_text(
        client, MODEL_NAME, REPLAN_PROMPT, user_prompt,
        temperature=0.4, max_tokens=2048,
    )


RESCOPE_PROMPT = """You are a Product Manager. A story has failed after multiple developer
retries and is unrecoverable with the current approach. You must decide how to proceed.

Options:
1. "simplify" -- produce a simpler version of this story that avoids the failing pattern.
   Include a complete simplified_story object with the same schema as the original.
2. "skip" -- this story is non-critical. Skip it and continue with the remaining stories.
3. "halt" -- this story is critical and cannot be simplified. Halt the pipeline.

Return JSON only:
{
  "decision": "simplify" | "skip" | "halt",
  "reason": "1-2 sentence explanation",
  "simplified_story": { ...story object... } or null
}"""


def rescope_story(story: Story, cumulative_errors: str, requirement_text: str) -> dict:
    """PM decides whether to simplify, skip, or halt for an unrecoverable story."""
    from agents.llm_client import call_llm_json

    client = make_openai_client()
    user_prompt = f"""Unrecoverable story:
  Title: {story.title}
  Description: {story.description}
  Ownership: {story.ownership}
  Acceptance Criteria: {json.dumps(story.acceptance_criteria)}
  Implementation Notes: {json.dumps(story.implementation_notes)}

Cumulative error history:
{cumulative_errors[:4000]}

Original requirement (truncated):
{requirement_text[:1500]}

Decide: simplify, skip, or halt. Return JSON."""

    return call_llm_json(
        client, MODEL_NAME, RESCOPE_PROMPT, user_prompt,
        temperature=0.3,
        max_retries=MAX_JSON_PARSE_RETRIES,
        retry_prompt=JSON_RETRY_PROMPT,
        on_raw_response=_store_raw_response,
    )


ENHANCEMENT_PROMPT = """You are a Product Manager. Given an enhancement request for an
EXISTING codebase, produce exactly ONE implementation-ready user story.

The enhancement is NOT a new project -- it modifies or extends code that already exists
in the workspace.

Output JSON only. No markdown, no explanation. Format:
{
  "story": {
    "id": "enhance_1",
    "title": "Short title",
    "description": "One sentence",
    "acceptance_criteria": ["AC1", "AC2"],
    "implementation_notes": ["Important design note"],
    "test_focus": ["What must be tested"],
    "ownership": "<AGENT_TYPE>",
    "dependencies": []
  }
}

Rules:
- ownership MUST be set to the specified agent type
- The story should work against existing code, not create a new project from scratch
- Implementation notes should reference modifying existing files/components
- Keep the story focused on the specific enhancement requested
- Acceptance criteria must be observable and testable"""

ENHANCEMENT_BOTH_PROMPT = """You are a Product Manager. Given an enhancement request for an
EXISTING codebase, produce exactly TWO implementation-ready user stories:
one for the backend agent and one for the frontend agent.

The enhancement is NOT a new project -- it modifies or extends code that already exists
in the workspace.

Output JSON only. No markdown, no explanation. Format:
{
  "stories": [
    {
      "id": "enhance_be",
      "title": "Backend: short title",
      "description": "One sentence",
      "acceptance_criteria": ["AC1", "AC2"],
      "implementation_notes": ["Important design note"],
      "test_focus": ["What must be tested"],
      "ownership": "backend",
      "dependencies": []
    },
    {
      "id": "enhance_fe",
      "title": "Frontend: short title",
      "description": "One sentence",
      "acceptance_criteria": ["AC1", "AC2"],
      "implementation_notes": ["Important design note"],
      "test_focus": ["What must be tested"],
      "ownership": "frontend",
      "dependencies": ["enhance_be"]
    }
  ]
}

Rules:
- First story MUST have ownership "backend", second MUST have ownership "frontend"
- The frontend story should depend on the backend story
- Stories should work against existing code, not create a new project from scratch
- Implementation notes should reference modifying existing files/components
- Keep each story focused on its respective layer
- Acceptance criteria must be observable and testable"""


def create_enhancement_story(agent_type: str, description: str, context: str = "") -> dict:
    """PM generates a single story from a free-text enhancement description."""
    from agents.llm_client import call_llm_json

    client = make_openai_client()
    prompt = ENHANCEMENT_PROMPT.replace("<AGENT_TYPE>", agent_type)

    user_text = f"""Enhancement request for the {agent_type} agent:

Description: {description}
"""
    if context:
        user_text += f"\nAdditional context:\n{context}\n"

    user_text += f"\nGenerate exactly 1 story with ownership = \"{agent_type}\"."

    data = call_llm_json(
        client, MODEL_NAME, prompt, user_text,
        temperature=0.3,
        max_retries=MAX_JSON_PARSE_RETRIES,
        retry_prompt=JSON_RETRY_PROMPT,
        on_raw_response=_store_raw_response,
    )

    story_data = data.get("story") or data.get("stories", [{}])[0]
    story_data["id"] = f"enhance_{uuid.uuid4().hex[:8]}"
    story_data["ownership"] = agent_type
    return story_data


def create_enhancement_stories_both(description: str, context: str = "") -> list[dict]:
    """PM generates a backend + frontend story pair for a full-stack enhancement."""
    from agents.llm_client import call_llm_json

    client = make_openai_client()

    user_text = f"""Enhancement request spanning both backend and frontend:

Description: {description}
"""
    if context:
        user_text += f"\nAdditional context:\n{context}\n"

    user_text += "\nGenerate exactly 2 stories: 1 backend, 1 frontend (frontend depends on backend)."

    data = call_llm_json(
        client, MODEL_NAME, ENHANCEMENT_BOTH_PROMPT, user_text,
        temperature=0.3,
        max_retries=MAX_JSON_PARSE_RETRIES,
        retry_prompt=JSON_RETRY_PROMPT,
        on_raw_response=_store_raw_response,
    )

    raw_stories = data.get("stories", [])
    result = []
    for s in raw_stories[:2]:
        s["id"] = f"enhance_{uuid.uuid4().hex[:8]}"
        result.append(s)

    if len(result) == 0:
        story = data.get("story", {})
        story["id"] = f"enhance_{uuid.uuid4().hex[:8]}"
        result.append(story)

    return result


def create_stories(requirement: Requirement) -> StoryPack:
    import state_store as _ss

    client = make_openai_client()

    past_patterns = _ss.get_failure_patterns(limit=10)
    learning_addendum = ""
    if past_patterns:
        lines = []
        for p in past_patterns:
            lines.append(
                f"- [{p['agent_type']}] {p['error_category']}: {p['root_cause']} -> Fix: {p['resolution']}"
            )
        learning_addendum = (
            "\n\nPast failure patterns to avoid (from previous runs):\n"
            + "\n".join(lines)
            + "\n\nDesign stories to avoid these known pitfalls."
        )

    prompt_text = requirement.text + learning_addendum
    data = _call_pm_llm(client, prompt_text)
    normalized = []
    for index, story in enumerate(data["stories"], start=1):
        item = dict(story)
        item["id"] = f"{requirement.id}_story_{index}"
        normalized.append(item)

    stories = [Story(**s) for s in normalized]
    return StoryPack(
        id=f"pack_{uuid.uuid4().hex[:8]}",
        requirement_id=requirement.id,
        requirement_text=requirement.text,
        stories=stories,
    )
