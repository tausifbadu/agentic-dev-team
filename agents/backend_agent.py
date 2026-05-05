"""Backend Agent. Planner-first patching mode with isolated validation."""

import json
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from openai import OpenAI

from agents.reasoning import (
    create_scratch_copy,
    load_json,
    load_skill_guidelines,
    promote_scratch_copy,
    render_context,
    save_json,
    select_relevant_files,
    update_scope,
    workspace_export_map,
    workspace_file_tree,
)
from schemas import Story

ProgressCallback = Callable[[str, str | None], None] | None


def _format_files_detail(patch: dict) -> str:
    parts: list[str] = []
    for f in patch.get("files", []):
        path = f.get("path", "unknown")
        content = f.get("content", "")
        parts.append(f"--- {path} ---\n{content}")
    return "\n\n".join(parts)

WORKSPACE = Path(__file__).parent.parent / "workspace"
BACKEND = WORKSPACE / "backend"
SCOPE_FILE = WORKSPACE / "scope.json"
DEBUG_DIR = WORKSPACE / "debug"

MODEL_NAME = os.getenv("BACKEND_AGENT_MODEL", "gpt-4o-mini")
MAX_RETRIES = 2
MAX_JSON_PARSE_RETRIES = 2

SKILL_FASTAPI_PATH = Path(__file__).parent.parent / ".cursor" / "skills" / "agentic-dev-team" / "SKILL_FASTAPI.md"
FASTAPI_GUIDELINES = load_skill_guidelines(SKILL_FASTAPI_PATH)

_PLANNER_BASE = """You are an implementation planner for a Python FastAPI backend.
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
- CRITICAL PYTHON 3.9 COMPATIBILITY: The runtime Python is 3.9. You MUST NOT use `X | Y` union syntax anywhere. Always use `Optional[X]` from typing for nullable types and `Union[X, Y]` for unions. Add `from typing import Optional, List, Tuple, Union` as needed. `from __future__ import annotations` does NOT fix this for FastAPI/Pydantic because they evaluate annotations at runtime.
- CRITICAL FILE AWARENESS: Only reference files that exist in the workspace file listing provided. Do NOT assume modules like `config`, `settings`, `database` exist unless they appear in the listing. If new modules are needed, include them in your plan.
- CRITICAL DEPENDENCY PRESERVATION: An export map shows each file's public functions/classes. When planning changes to an existing file, NEVER plan to remove or rename any existing public function or class — other files depend on them. Only plan to ADD or EXTEND code.
"""

PLANNER_PROMPT = (
    _PLANNER_BASE + f"\n\nFastAPI Engineering Guidelines:\n{FASTAPI_GUIDELINES}"
    if FASTAPI_GUIDELINES
    else _PLANNER_BASE
)

_CODER_BASE = """You are a Backend Developer. Extend the existing FastAPI codebase using the provided plan.

Hard constraints:
- Preserve existing behavior unless the story explicitly changes it.
- Use focused, additive patches only.
- Add all imported runtime dependencies to requirements.txt.
- Mock all external boundaries in tests. No real network calls.
- Use lifespan context managers, not deprecated FastAPI event hooks.
- Do not use `@router.exception_handler(...)` on `APIRouter`; handle route errors with local `try/except` or register exception handlers on the `FastAPI` app.
- CRITICAL PYTHON 3.9 COMPATIBILITY: The runtime Python is 3.9. You MUST NOT use `X | Y` union syntax (e.g. `str | None`, `dict | None`). Always use `Optional[str]`, `Optional[dict]`, `Union[X, Y]` from typing. Add `from typing import Optional, List, Tuple, Union` to every file. The `from __future__ import annotations` import does NOT fix this for FastAPI/Pydantic because they evaluate annotations at runtime.
- CRITICAL FILE AWARENESS: A listing of ALL files in the workspace is provided below the context. You MUST ONLY import from modules that actually exist in that listing. Do NOT invent or hallucinate modules like `config`, `settings`, `database`, `utils`, etc. unless they appear in the file listing. If you need new functionality, create the module yourself and include it in your "files" array.
- CRITICAL DEPENDENCY PRESERVATION: An export map showing every file's public functions/classes is provided. When you rewrite an existing file, you MUST keep ALL existing public functions and classes — other files import them. NEVER remove or rename a function unless the story explicitly requires it. Add new code alongside the existing exports.
- Each file MUST be a separate entry in the "files" array. NEVER concatenate multiple
  files into one "content" string. NEVER use === separators between files.
- Output JSON only:
{
  "files": [{"path": "relative/path/from/backend", "content": "full file content"}],
  "requirements_add": ["package1"]
}
"""

CODER_PROMPT = (
    _CODER_BASE + f"\n\nFastAPI Engineering Guidelines:\n{FASTAPI_GUIDELINES}"
    if FASTAPI_GUIDELINES
    else _CODER_BASE
)

JSON_RETRY_PROMPT = """Your previous response was not valid JSON. Return ONLY valid JSON matching the requested schema. No markdown fences. No commentary."""
PYTEST_RETRY_PROMPT = """pytest failed. First classify whether the issue is in tests, implementation, or dependencies. Fix the smallest correct layer and return corrected JSON only."""


def _store_raw_response(prefix: str, raw: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    out = DEBUG_DIR / f"{prefix}_{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}.txt"
    out.write_text(raw, encoding="utf-8")


def _call_llm_json(client: OpenAI, prompt: str, system_prompt: str) -> dict:
    from agents.llm_client import call_llm_json
    print(f"[backend_agent] LLM call: {len(system_prompt) + len(prompt)} chars input, model={MODEL_NAME}", flush=True)
    return call_llm_json(
        client, MODEL_NAME, system_prompt, prompt,
        max_retries=MAX_JSON_PARSE_RETRIES,
        retry_prompt=JSON_RETRY_PROMPT,
        on_raw_response=lambda raw: _store_raw_response("backend_json", raw),
    )


MAX_REQ_CHARS = 2000


def _story_bundle(story: Story, requirement_text: str, sibling_stories: list[Story]) -> str:
    truncated = requirement_text[:MAX_REQ_CHARS] + ("..." if len(requirement_text) > MAX_REQ_CHARS else "")
    siblings = [
        {
            "id": item.id,
            "title": item.title,
            "ownership": item.ownership,
            "acceptance_criteria": item.acceptance_criteria,
            "implementation_notes": item.implementation_notes,
            "test_focus": item.test_focus,
        }
        for item in sibling_stories
        if item.id != story.id
    ]
    return json.dumps(
        {
            "requirement": truncated,
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


def _plan_backend_change(
    client: OpenAI,
    story: Story,
    requirement_text: str,
    sibling_stories: list[Story],
    fix_context: str = "",
) -> dict:
    print(f"[backend_agent] Building plan prompt for: {story.title}", flush=True)
    bundle = _story_bundle(story, requirement_text, sibling_stories)
    print(f"[backend_agent] Story bundle: {len(bundle)} chars", flush=True)
    ctx = _focused_backend_context(story, requirement_text, sibling_stories)
    print(f"[backend_agent] Backend context: {len(ctx)} chars", flush=True)
    fix_block = f"\n\nPM Fix / Replan Guidance (incorporate into your plan):\n{fix_context}\n" if fix_context else ""
    file_tree = workspace_file_tree(BACKEND, {".py"})
    export_map = workspace_export_map(BACKEND, {".py"})
    export_section = f"\n\nEXPORT MAP (each file's public functions/classes — you MUST NOT remove any of these):\n{export_map}" if export_map else ""
    prompt = f"""Planning payload:
{bundle}

Focused backend context:
{ctx}

ALL files currently in the backend workspace (only import from these):
{file_tree}
{export_section}
{fix_block}"""
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
    file_tree = workspace_file_tree(BACKEND, {".py"})
    export_map = workspace_export_map(BACKEND, {".py"})
    export_section = f"\n\nEXPORT MAP (each file's public functions/classes — PRESERVE ALL of these when rewriting files):\n{export_map}" if export_map else ""
    prompt = f"""Implementation payload:
{_story_bundle(story, requirement_text, sibling_stories)}

Approved plan:
{json.dumps(plan, indent=2)}

Focused backend context:
{_focused_backend_context(story, requirement_text, sibling_stories)}

ALL files currently in the backend workspace (only import from these — do NOT invent modules):
{file_tree}
{export_section}
{retry_block}
Return only the files you modify/create and any new requirements.
When rewriting an existing file, you MUST include ALL its existing public functions/classes (see export map above) in your output — even if your story does not modify them. Dropping them will break other files that import them.
"""
    if error_output:
        return _call_llm_json(client, prompt + "\n" + PYTEST_RETRY_PROMPT, CODER_PROMPT)
    return _call_llm_json(client, prompt, CODER_PROMPT)


_FILE_SEP_RE = re.compile(r"^={3,}\s*(.+?)\s*={3,}\s*$", re.MULTILINE)


def _sanitize_patch(data: dict) -> dict:
    """Split concatenated file entries that the LLM joined with === separators."""
    clean_files: list[dict] = []
    for entry in data.get("files", []):
        content = entry.get("content", "")
        parts = _FILE_SEP_RE.split(content)
        if len(parts) <= 1:
            clean_files.append(entry)
            continue
        # parts = [before_first_sep, sep_filename, after_sep, sep2, after_sep2, ...]
        leading = parts[0].rstrip()
        if leading:
            clean_files.append({"path": entry["path"], "content": leading})
        for i in range(1, len(parts), 2):
            sep_path = parts[i].strip()
            body = parts[i + 1].strip() if i + 1 < len(parts) else ""
            if body:
                clean_files.append({"path": sep_path, "content": body})
    data["files"] = clean_files
    return data


def _apply_changes(data: dict, root: Path) -> None:
    data = _sanitize_patch(data)
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
    # Exit code 5 = no tests collected, which is fine for implementation-only stories
    if result.returncode not in (0, 5):
        return False, f"{result.stderr}\n{result.stdout}"
    return True, ""


def implement_backend(
    story: Story,
    requirement_text: str = "",
    sibling_stories: list[Story] | None = None,
    on_progress: ProgressCallback = None,
    fix_context: str = "",
    replan: bool = False,
) -> tuple[bool, str]:
    """Plan, patch, validate in scratch, and promote only on success."""
    client = OpenAI(timeout=90)
    siblings = sibling_stories or []
    last_error = fix_context
    emit = on_progress or (lambda msg, detail=None: None)

    if fix_context:
        emit("Fix mode: injecting error context from previous failure")
    if replan:
        emit("Replan mode: PM has provided a revised implementation strategy")

    for attempt in range(MAX_RETRIES + 1):
        emit(f"Planning implementation (attempt {attempt + 1})...")
        try:
            plan = _plan_backend_change(
                client, story, requirement_text, siblings,
                fix_context=fix_context if (replan or attempt == 0) else "",
            )
            emit("Plan ready", json.dumps(plan, indent=2))

            emit("Generating code...")
            patch = _generate_backend_patch(client, story, requirement_text, siblings, plan, last_error)
            file_paths = [f.get("path", "?") for f in patch.get("files", [])]
            emit(f"Code generated: {', '.join(file_paths)}", _format_files_detail(patch))
        except ValueError as exc:
            return False, str(exc)

        emit("Validating with pytest...")
        scratch = create_scratch_copy(BACKEND, "backend_attempt")
        _apply_changes(patch, scratch)
        ok, error_output = _run_pytest(scratch)
        if ok:
            emit("Validation passed")
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
        emit("Validation failed, retrying...", error_output)
        last_error = error_output

    return False, f"pytest failed after {MAX_RETRIES + 1} attempts:\n{last_error}"
