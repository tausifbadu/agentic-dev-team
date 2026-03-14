"""Frontend Agent. Planner-first React patching with isolated validation."""

import json
import os
import shutil
import subprocess
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
FRONTEND = WORKSPACE / "frontend"
SCOPE_FILE = WORKSPACE / "scope.json"
DEBUG_DIR = WORKSPACE / "debug"

MODEL_NAME = os.getenv("FRONTEND_AGENT_MODEL", "gpt-4o-mini")
MAX_RETRIES = 2
MAX_JSON_PARSE_RETRIES = 2

PLANNER_PROMPT = """You are a frontend implementation planner for a React app.
Return JSON only:
{
  "summary": "short plan summary",
  "files_to_touch": ["src/App.jsx", "src/styles.css"],
  "acceptance_map": [{"criterion": "AC", "implementation": "UI behavior"}],
  "validation_steps": ["build", "runtime states"],
  "risk_checks": ["regressions to avoid"]
}

Rules:
- Prefer additive changes.
- Map every acceptance criterion to a concrete UI state or interaction.
- If backend data is needed, use the backend API contract rather than assuming direct webhook access in the browser.
"""

CODER_PROMPT = """You are a Frontend Developer extending an existing React app.

Hard constraints:
- Preserve existing functionality.
- Use focused additive patches only.
- Provide visible loading, error, and data states when data fetching is involved.
- Keep the UI simple and local-dev friendly.
- Output JSON only:
{
  "files": [{"path": "relative/path/from/frontend", "content": "full file content"}],
  "dependencies_add": ["package1"],
  "dev_dependencies_add": ["package2"]
}
"""

JSON_RETRY_PROMPT = """Your previous response was not valid JSON. Return ONLY valid JSON matching the requested schema. No markdown fences. No commentary."""
BUILD_RETRY_PROMPT = """Frontend checks failed. First determine whether the issue is in build config, runtime assumptions, or component code. Fix the smallest correct layer and return corrected JSON only."""


def _store_raw_response(raw: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    out = DEBUG_DIR / f"frontend_raw_{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}.txt"
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
            _store_raw_response(content)
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": JSON_RETRY_PROMPT})
    raise ValueError("Invalid JSON from frontend model after retries: " + " | ".join(errors))


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


def _read_existing_frontend(story: Story, requirement_text: str) -> str:
    text = " ".join(
        [
            requirement_text,
            story.title,
            story.description,
            *story.acceptance_criteria,
            *story.implementation_notes,
            *story.test_focus,
        ]
    )
    files = select_relevant_files(FRONTEND, text, {".js", ".jsx", ".ts", ".tsx", ".css", ".html", ".json"}, max_files=10)
    return render_context(FRONTEND, files)


def _plan_frontend_change(client: OpenAI, story: Story, requirement_text: str, sibling_stories: list[Story]) -> dict:
    prompt = f"""Planning payload:
{_story_bundle(story, requirement_text, sibling_stories)}

Focused frontend context:
{_read_existing_frontend(story, requirement_text)}
"""
    return _call_llm_json(client, prompt, PLANNER_PROMPT)


def _generate_frontend_patch(
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

Focused frontend context:
{_read_existing_frontend(story, requirement_text)}
{retry_block}
Return only changed files and dependency additions.
"""
    if error_output:
        return _call_llm_json(client, prompt + "\n" + BUILD_RETRY_PROMPT, CODER_PROMPT)
    return _call_llm_json(client, prompt, CODER_PROMPT)


def _ensure_package_json(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    pkg = root / "package.json"
    if pkg.exists():
        return
    pkg.write_text(
        json.dumps(
            {
                "name": "frontend",
                "private": True,
                "version": "0.1.0",
                "type": "module",
                "scripts": {"dev": "vite", "build": "vite build", "preview": "vite preview"},
                "dependencies": {"react": "^18.3.1", "react-dom": "^18.3.1"},
                "devDependencies": {"vite": "^5.4.0"},
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _merge_deps(pkg_path: Path, deps: list[str], dev_deps: list[str]) -> None:
    pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    pkg.setdefault("dependencies", {})
    pkg.setdefault("devDependencies", {})

    for item in deps:
        if "@" in item and not item.startswith("@"):
            name, version = item.split("@", 1)
            pkg["dependencies"][name] = version
        else:
            pkg["dependencies"].setdefault(item, "latest")

    for item in dev_deps:
        if "@" in item and not item.startswith("@"):
            name, version = item.split("@", 1)
            pkg["devDependencies"][name] = version
        else:
            pkg["devDependencies"].setdefault(item, "latest")

    pkg_path.write_text(json.dumps(pkg, indent=2), encoding="utf-8")


def _apply_changes(data: dict, root: Path) -> None:
    _ensure_package_json(root)
    for file_change in data.get("files", []):
        path = root / file_change["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(file_change["content"], encoding="utf-8")
    _merge_deps(root / "package.json", data.get("dependencies_add", []), data.get("dev_dependencies_add", []))


def _npm_command() -> list[str] | None:
    npm_exe = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm_exe:
        return None
    if npm_exe.lower().endswith(".cmd"):
        return ["cmd", "/c", npm_exe]
    return [npm_exe]


def _run_frontend_checks(root: Path) -> tuple[bool, str]:
    npm_cmd = _npm_command()
    if not npm_cmd:
        return False, "npm not found. Install Node.js and ensure npm is available in PATH for this Python process."

    install = subprocess.run([*npm_cmd, "install"], cwd=root, capture_output=True, text=True, check=False)
    if install.returncode != 0:
        return False, f"npm install failed:\n{install.stderr}\n{install.stdout}"

    build = subprocess.run([*npm_cmd, "run", "build"], cwd=root, capture_output=True, text=True, check=False)
    if build.returncode != 0:
        return False, f"npm run build failed:\n{build.stderr}\n{build.stdout}"

    dist_index = root / "dist" / "index.html"
    if not dist_index.exists():
        return False, "npm run build succeeded but dist/index.html was not created."
    return True, ""


def implement_frontend(
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
            plan = _plan_frontend_change(client, story, requirement_text, siblings)
            patch = _generate_frontend_patch(client, story, requirement_text, siblings, plan, last_error)
        except ValueError as exc:
            return False, str(exc)

        scratch = create_scratch_copy(FRONTEND, "frontend_attempt")
        _apply_changes(patch, scratch)
        ok, output = _run_frontend_checks(scratch)
        if ok:
            promote_scratch_copy(scratch, FRONTEND)
            scope = update_scope(
                SCOPE_FILE,
                {
                    "id": story.id,
                    "title": story.title,
                    "owner": "frontend",
                    "summary": plan.get("summary", story.description),
                    "applied_at": datetime.utcnow().isoformat(),
                },
            )
            save_json(SCOPE_FILE, scope)
            retry_note = f" (attempt {attempt + 1})" if attempt > 0 else ""
            return True, f"Patched {FRONTEND}. build passed{retry_note}. Scope entries: {len(scope['implemented_stories'])}"
        last_error = output

    return False, f"frontend checks failed after {MAX_RETRIES + 1} attempts:\n{last_error}"




