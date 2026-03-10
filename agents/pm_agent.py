"""PM Agent. One LLM call. No tools. Returns StoryPack."""

import json
import uuid

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


def create_stories(requirement: Requirement) -> StoryPack:
    client = OpenAI()
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": requirement.text},
        ],
        temperature=0.3,
    )
    content = response.choices[0].message.content.strip()
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
    data = json.loads(content)
    stories = [Story(**s) for s in data["stories"]]
    return StoryPack(
        id=f"pack_{uuid.uuid4().hex[:8]}",
        requirement_id=requirement.id,
        requirement_text=requirement.text,
        stories=stories,
    )
