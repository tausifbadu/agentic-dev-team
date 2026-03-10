"""Backend Agent. Input: Story. Output: code + pytest. Must pass tests."""

import json
import subprocess
import sys
from pathlib import Path

from openai import OpenAI

from schemas import Story

WORKSPACE = Path(__file__).parent.parent / "workspace" / "backend"

SYSTEM_PROMPT = """You are a Backend Developer. Implement the given user story in Python FastAPI.

Output JSON only. Format:
{
  "files": [
    {"path": "main.py", "content": "..."},
    {"path": "tests/test_main.py", "content": "..."}
  ]
}

Rules:
- Use FastAPI, Pydantic
- Include tests that verify the story's acceptance criteria
- main.py must have: app = FastAPI(), and the route(s) described in the story
- Tests use TestClient from fastapi.testclient
- No placeholder code. Working implementation."""


def implement_backend(story: Story) -> tuple[bool, str]:
    """Implement story. Returns (success, message). Runs pytest before considering done."""
    client = OpenAI()
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Story: {story.title}\n{story.description}\nAC: {story.acceptance_criteria}",
            },
        ],
        temperature=0.2,
    )
    content = response.choices[0].message.content.strip()
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
    data = json.loads(content)
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    (WORKSPACE / "tests").mkdir(exist_ok=True)
    for f in data["files"]:
        path = WORKSPACE / f["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f["content"], encoding="utf-8")
    req_path = WORKSPACE / "requirements.txt"
    if not req_path.exists():
        req_path.write_text("fastapi\nuvicorn\npytest\nhttpx\n")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"],
        cwd=WORKSPACE,
        capture_output=True,
        check=False,
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-v", str(WORKSPACE)],
        capture_output=True,
        text=True,
        cwd=WORKSPACE,
    )
    if result.returncode != 0:
        return False, f"pytest failed:\n{result.stderr}\n{result.stdout}"
    return True, f"Code written to {WORKSPACE}. pytest passed."
