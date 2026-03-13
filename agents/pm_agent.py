"""PM Agent. One LLM call. No tools. Returns StoryPack."""

import json
import uuid
from datetime import datetime
from pathlib import Path

from openai import OpenAI

from schemas import Requirement, Story, StoryPack

SYSTEM_PROMPT = """You are a Product Manager. Given a requirement, produce user stories.

Output JSON only. No markdown, no explanation. Format:
{
  "stories": [
    {
      "id": "story_1",
      "title": "Short title",
      "description": "One sentence",
      "acceptance_criteria": ["AC1", "AC2"],
      "ownership": "frontend" or "backend"
    }
  ]
}

Rules:
- Each story has one ownership: frontend or backend
- Acceptance criteria must be testable
- Keep stories small and focused
- Use ids: story_1, story_2, ..."""

JSON_RETRY_PROMPT = """Your previous response was not valid JSON. Return ONLY valid JSON matching this schema:
{
  "stories": [
    {
      "id": "story_1",
      "title": "Short title",
      "description": "One sentence",
      "acceptance_criteria": ["AC1", "AC2"],
      "ownership": "frontend" or "backend"
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
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": requirement_text},
    ]
    errors: list[str] = []

    for _ in range(MAX_JSON_PARSE_RETRIES + 1):
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        content = (response.choices[0].message.content or "").strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()

        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            errors.append(str(e))
            _store_raw_response(content)
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": JSON_RETRY_PROMPT})

    raise ValueError("Invalid JSON from PM model after retries: " + " | ".join(errors))


def create_stories(requirement: Requirement) -> StoryPack:
    client = OpenAI()
    data = _call_pm_llm(client, requirement.text)
    stories = [Story(**s) for s in data["stories"]]
    return StoryPack(
        id=f"pack_{uuid.uuid4().hex[:8]}",
        requirement_id=requirement.id,
        requirement_text=requirement.text,
        stories=stories,
    )
