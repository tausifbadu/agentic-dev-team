"""Frontend Agent. Patching mode for React/HTML/CSS. Returns (success, message)."""

import json
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

from openai import OpenAI

from schemas import Story

WORKSPACE = Path(__file__).parent.parent / "workspace"
FRONTEND = WORKSPACE / "frontend"
SCOPE_FILE = WORKSPACE / "scope.json"
DEBUG_DIR = WORKSPACE / "debug"

SYSTEM_PROMPT = """You are a Frontend Developer. EXTEND the existing frontend codebase to implement the new story. Do NOT remove or break existing functionality.

Tech stack requirements:
- React + JavaScript/TypeScript
- HTML/CSS for UI styling
- Keep implementation simple, readable, and local-dev friendly

Workspace expectations:
- frontend/package.json
- frontend/src/... for React app code
- frontend/index.html

Rules:
- Preserve existing code; patch/add only required files.
- Use additive changes; avoid rewriting unrelated files.
- If story asks for live crude oil values, consume backend ticker endpoint by polling `/ticker` every few seconds unless story explicitly says otherwise.
- If webhook is mentioned in UI story, treat it as data source context and display data from backend API unless a direct browser-safe stream endpoint is explicitly provided.
- Include minimal styling (CSS) and clear user-visible states: loading, error, data.
- Output JSON only. Format:
{
  "files": [
    {"path": "relative/path/from/frontend", "content": "full file content"}
  ],
  "dependencies_add": ["package1", "package2"],
  "dev_dependencies_add": ["packageA"]
}
- Only include files you modify/create.
- Ensure app builds with `npm run build`.
"""

JSON_RETRY_PROMPT = """Your previous response was not valid JSON. Return ONLY valid JSON matching this schema:
{
  "files": [{"path": "relative/path", "content": "full file content"}],
  "dependencies_add": ["pkg"],
  "dev_dependencies_add": ["pkg"]
}
No markdown fences. No commentary."""

RETRY_PROMPT = """Frontend checks failed. Fix code/build issues using this output and return corrected JSON only."""

MAX_RETRIES = 2
MAX_JSON_PARSE_RETRIES = 2


def _load_scope() -> dict:
    if not SCOPE_FILE.exists():
        return {"implemented_stories": [], "last_updated": None}
    with open(SCOPE_FILE, encoding="utf-8") as f:
        return json.load(f)


def _save_scope(scope: dict) -> None:
    SCOPE_FILE.parent.mkdir(parents=True, exist_ok=True)
    scope["last_updated"] = datetime.utcnow().isoformat()
    with open(SCOPE_FILE, "w", encoding="utf-8") as f:
        json.dump(scope, f, indent=2)


def _store_raw_response(raw: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    out = DEBUG_DIR / f"frontend_raw_{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}.txt"
    out.write_text(raw, encoding="utf-8")


def _read_existing_frontend() -> str:
    if not FRONTEND.exists():
        return "(empty frontend workspace)"
    include_ext = {".js", ".jsx", ".ts", ".tsx", ".css", ".html", ".json", ".md"}
    lines = []
    for p in sorted(FRONTEND.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in include_ext:
            continue
        if "node_modules" in str(p):
            continue
        rel = p.relative_to(FRONTEND)
        lines.append(f"=== {rel} ===\n{p.read_text(encoding='utf-8')}\n")
    return "\n".join(lines) if lines else "(empty frontend workspace)"


def _call_llm(client: OpenAI, messages: list[dict]) -> dict:
    local_messages = list(messages)
    errors: list[str] = []

    for _ in range(MAX_JSON_PARSE_RETRIES + 1):
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=local_messages,
            temperature=0.2,
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
            local_messages.append({"role": "assistant", "content": content})
            local_messages.append({"role": "user", "content": JSON_RETRY_PROMPT})

    raise ValueError("Invalid JSON from frontend model after retries: " + " | ".join(errors))


def _ensure_package_json() -> None:
    FRONTEND.mkdir(parents=True, exist_ok=True)
    pkg = FRONTEND / "package.json"
    if pkg.exists():
        return
    pkg.write_text(
        json.dumps(
            {
                "name": "frontend",
                "private": True,
                "version": "0.1.0",
                "type": "module",
                "scripts": {
                    "dev": "vite",
                    "build": "vite build",
                    "preview": "vite preview"
                },
                "dependencies": {
                    "react": "^18.3.1",
                    "react-dom": "^18.3.1"
                },
                "devDependencies": {
                    "vite": "^5.4.0"
                }
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


def _apply_changes(data: dict) -> None:
    _ensure_package_json()

    for f in data.get("files", []):
        path = FRONTEND / f["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f["content"], encoding="utf-8")

    pkg_path = FRONTEND / "package.json"
    _merge_deps(pkg_path, data.get("dependencies_add", []), data.get("dev_dependencies_add", []))



def _npm_command() -> list[str] | None:
    """Return a runnable npm command across Windows/macOS/Linux."""
    npm_exe = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm_exe:
        return None

    # npm on Windows is usually a .cmd shim; execute via cmd for reliability.
    if npm_exe.lower().endswith(".cmd"):
        return ["cmd", "/c", npm_exe]
    return [npm_exe]
def _run_frontend_checks() -> tuple[bool, str]:
    npm_cmd = _npm_command()
    if not npm_cmd:
        return (
            False,
            "npm not found. Install Node.js and ensure npm is available in PATH for this Python process.",
        )

    install = subprocess.run(
        [*npm_cmd, "install"],
        cwd=FRONTEND,
        capture_output=True,
        text=True,
        check=False,
    )
    if install.returncode != 0:
        return False, f"npm install failed:\n{install.stderr}\n{install.stdout}"

    build = subprocess.run(
        [*npm_cmd, "run", "build"],
        cwd=FRONTEND,
        capture_output=True,
        text=True,
        check=False,
    )
    if build.returncode != 0:
        return False, f"npm run build failed:\n{build.stderr}\n{build.stdout}"

    return True, ""

def implement_frontend(story: Story) -> tuple[bool, str]:
    client = OpenAI()
    scope = _load_scope()
    existing = _read_existing_frontend()

    user_content = f"""Existing frontend codebase:
{existing}

---
NEW STORY to implement:
Title: {story.title}
Description: {story.description}
Acceptance criteria: {story.acceptance_criteria}

Extend the frontend codebase. Preserve existing functionality. Output JSON with files/dependencies."""

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    for attempt in range(MAX_RETRIES + 1):
        try:
            data = _call_llm(client, messages)
        except ValueError as e:
            if attempt < MAX_RETRIES:
                messages.append({"role": "user", "content": f"{JSON_RETRY_PROMPT}\n\nError: {e}"})
                continue
            return False, f"Frontend JSON parsing failed after {MAX_RETRIES + 1} attempts: {e}"

        _apply_changes(data)
        ok, output = _run_frontend_checks()

        if ok:
            implemented = scope.get("implemented_stories", [])
            if story.id not in implemented:
                implemented.append(story.id)
                scope["implemented_stories"] = implemented
            _save_scope(scope)
            retry_note = f" (attempt {attempt + 1})" if attempt > 0 else ""
            return True, f"Patched {FRONTEND}. build passed{retry_note}. Scope: {scope['implemented_stories']}"

        if attempt < MAX_RETRIES:
            messages.append({"role": "assistant", "content": json.dumps(data)})
            messages.append({"role": "user", "content": f"{RETRY_PROMPT}\n\nBuild output:\n{output}"})
        else:
            return False, f"frontend checks failed after {MAX_RETRIES + 1} attempts:\n{output}"




