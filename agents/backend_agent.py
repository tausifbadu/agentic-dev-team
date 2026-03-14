"""Backend Agent. Planner-first patching mode with isolated validation."""

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

from openai import OpenAI

from agents.reasoning import (
    create_scratch_copy,
    load_json,
    promote_scratch_copy,
    render_context,
    save_json,
    select_relevant_files,
    update_scope,
)
from schemas import Story

WORKSPACE = Path(__file__).parent.parent / "workspace"
BACKEND = WORKSPACE / "backend"
SCOPE_FILE = WORKSPACE / "scope.json"
DEBUG_DIR = WORKSPACE / "debug"

MODEL_NAME = os.getenv("BACKEND_AGENT_MODEL", "gpt-4o-mini")
MAX_RETRIES = 2
MAX_JSON_PARSE_RETRIES = 2

PLANNER_PROMPT = """You are an implementation planner for a Python FastAPI backend.
Return JSON only with this schema:
{
  "summary": "short implementation summary",
  "files_to_touch": ["main.py", "routers/example.py"],
  "acceptance_map": [{"criterion": "AC", "implementation": "how it will be satisfied"}],
  "validation_steps": ["pytest target or smoke check"],
  "risk_checks": ["specific regressions to avoid"]
}

Rules:
- Plan additive changes that preserve existing behavior.
- Touch the fewest files possible.
- If the story changes a read-only endpoint, do not invent side effects.
- If tests are needed, map every acceptance criterion to at least one verification path.
"""

CODER_PROMPT = """You are a Backend Developer. Extend the existing FastAPI codebase using the provided plan.

Hard constraints:
- Preserve existing behavior unless the story explicitly changes it.
- Use focused, additive patches only.
- Add all imported runtime dependencies to requirements.txt.
- Mock all external boundaries in tests. No real network calls.
- Use lifespan context managers, not deprecated FastAPI event hooks.
- Do not use `@router.exception_handler(...)` on `APIRouter`; handle route errors with local `try/except` or register exception handlers on the `FastAPI` app.
- Output JSON only:
{
  "files": [{"path": "relative/path/from/backend", "content": "full file content"}],
  "requirements_add": ["package1"]
}
"""

JSON_RETRY_PROMPT = """Your previous response was not valid JSON. Return ONLY valid JSON matching the requested schema. No markdown fences. No commentary."""
PYTEST_RETRY_PROMPT = """pytest failed. First classify whether the issue is in tests, implementation, or dependencies. Fix the smallest correct layer and return corrected JSON only."""


def _store_raw_response(prefix: str, raw: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    out = DEBUG_DIR / f"{prefix}_{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}.txt"
    out.write_text(raw, encoding="utf-8")


def _call_llm_json(client: OpenAI, prompt: str, system_prompt: str) -> dict:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]
    errors: list[str] = []
    for _ in range(MAX_JSON_PARSE_RETRIES + 1):
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        content = (response.choices[0].message.content or "").strip()
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            errors.append(str(exc))
            _store_raw_response("backend_json", content)
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": JSON_RETRY_PROMPT})
    raise ValueError("Invalid JSON from backend model after retries: " + " | ".join(errors))


def _story_bundle(story: Story, requirement_text: str, sibling_stories: list[Story]) -> str:
    siblings = [
        {
            "id": item.id,
            "title": item.title,
            "ownership": item.ownership,
            "acceptance_criteria": item.acceptance_criteria,
        }
        for item in sibling_stories
        if item.id != story.id
    ]
    return json.dumps(
        {
            "requirement": requirement_text,
            "story": story.model_dump(),
            "sibling_stories": siblings,
            "implemented_scope": load_json(SCOPE_FILE, {"implemented_stories": []}),
        },
        indent=2,
    )


def _focused_backend_context(story: Story, requirement_text: str, sibling_stories: list[Story]) -> str:
    story_text = " ".join(
        [
            requirement_text,
            story.title,
            story.description,
            *story.acceptance_criteria,
            *story.implementation_notes,
            *story.test_focus,
        ]
    )
    files = select_relevant_files(BACKEND, story_text, {".py"}, max_files=10)
    return render_context(BACKEND, files)


def _plan_backend_change(client: OpenAI, story: Story, requirement_text: str, sibling_stories: list[Story]) -> dict:
    prompt = f"""Planning payload:
{_story_bundle(story, requirement_text, sibling_stories)}

Focused backend context:
{_focused_backend_context(story, requirement_text, sibling_stories)}
"""
    return _call_llm_json(client, prompt, PLANNER_PROMPT)


def _generate_backend_patch(
    client: OpenAI,
    story: Story,
    requirement_text: str,
    sibling_stories: list[Story],
    plan: dict,
    error_output: str = "",
) -> dict:
    retry_block = f"\nPrevious validation failure:\n{error_output}\n" if error_output else ""
    prompt = f"""Implementation payload:
{_story_bundle(story, requirement_text, sibling_stories)}

Approved plan:
{json.dumps(plan, indent=2)}

Focused backend context:
{_focused_backend_context(story, requirement_text, sibling_stories)}
{retry_block}
Return only the files you modify/create and any new requirements.
"""
    if error_output:
        return _call_llm_json(client, prompt + "\n" + PYTEST_RETRY_PROMPT, CODER_PROMPT)
    return _call_llm_json(client, prompt, CODER_PROMPT)


def _apply_changes(data: dict, root: Path) -> None:
    for file_change in data.get("files", []):
        path = root / file_change["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(file_change["content"], encoding="utf-8")

    req_path = root / "requirements.txt"
    if not req_path.exists():
        req_path.write_text("fastapi\nuvicorn\npytest\nhttpx\n", encoding="utf-8")
    existing = req_path.read_text(encoding="utf-8")
    for package in data.get("requirements_add", []):
        if package not in existing:
            existing = existing.rstrip() + f"\n{package}\n"
    req_path.write_text(existing, encoding="utf-8")


def _run_pytest(root: Path) -> tuple[bool, str]:
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-v", str(root)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False, f"{result.stderr}\n{result.stdout}"
    return True, ""


def implement_backend(
    story: Story,
    requirement_text: str = "",
    sibling_stories: list[Story] | None = None,
) -> tuple[bool, str]:
    """Plan, patch, validate in scratch, and promote only on success."""
    client = OpenAI()
    siblings = sibling_stories or []
    last_error = ""

    for attempt in range(MAX_RETRIES + 1):
        try:
            plan = _plan_backend_change(client, story, requirement_text, siblings)
            patch = _generate_backend_patch(client, story, requirement_text, siblings, plan, last_error)
        except ValueError as exc:
            return False, str(exc)

        scratch = create_scratch_copy(BACKEND, "backend_attempt")
        _apply_changes(patch, scratch)
        ok, error_output = _run_pytest(scratch)
        if ok:
            promote_scratch_copy(scratch, BACKEND)
            scope = update_scope(
                SCOPE_FILE,
                {
                    "id": story.id,
                    "title": story.title,
                    "owner": "backend",
                    "summary": plan.get("summary", story.description),
                    "applied_at": datetime.utcnow().isoformat(),
                },
            )
            save_json(SCOPE_FILE, scope)
            retry_note = f" (attempt {attempt + 1})" if attempt > 0 else ""
            return True, f"Patched {BACKEND}. pytest passed{retry_note}. Scope entries: {len(scope['implemented_stories'])}"
        last_error = error_output

    return False, f"pytest failed after {MAX_RETRIES + 1} attempts:\n{last_error}"
