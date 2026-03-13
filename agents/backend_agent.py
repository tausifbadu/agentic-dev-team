"""Backend Agent. Patching mode: extends existing codebase. Returns (success, message)."""

import json
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

from openai import OpenAI

from schemas import Story

WORKSPACE = Path(__file__).parent.parent / "workspace"
BACKEND = WORKSPACE / "backend"
SCOPE_FILE = WORKSPACE / "scope.json"
DEBUG_DIR = WORKSPACE / "debug"

SYSTEM_PROMPT = """You are a Backend Developer. EXTEND the existing codebase to implement the new story. Do NOT remove or break existing functionality.

Workspace structure:
- main.py: app entry, includes routers via app.include_router(...)
- routers/<feature>.py: APIRouter for each domain (e.g. routers/ticker.py, routers/health.py)
- services/: business logic
- models/: Pydantic models
- tests/test_<feature>.py: tests per feature

Rules:
- PRESERVE all existing code. Add new routers, routes, services. Update main.py to include new routers.
- Use FastAPI, Pydantic, APScheduler for background tasks. NO fastapi-utils.
- Add ALL imported packages to requirements.txt (including httpx).
- Mock ALL external HTTP in tests (yfinance, httpx.post, httpx.get). Tests must not hit real networks.
- Use lifespan context manager (NOT on_event). Each router can have start/stop helpers called from main lifespan.
- Output JSON only. Format:
{
  "files": [
    {"path": "relative/path/from/backend", "content": "full file content"}
  ],
  "requirements_add": ["package1", "package2"]
}

Only include files you modify or create. For new features: create routers/<name>.py, add tests, update main.py.

WEBHOOK FORWARDING (when story involves "forward to webhook" or "POST to webhook"):
- Read WEBHOOK_URL from os.environ. Only POST if WEBHOOK_URL is set.
- After fetching data (e.g. price), POST JSON payload to WEBHOOK_URL using httpx: {"symbol": str, "price": float, "timestamp": str (ISO format)}.
- Use httpx.post(webhook_url, json=payload, timeout=10.0). Catch and log errors; do not fail the fetch if webhook fails.
- In tests: mock httpx.post (e.g. monkeypatch.setattr) so no real HTTP. Assert the mock was called with expected payload.
- Add httpx to requirements_add if not present.


GENERIC QUALITY CONTRACT (MANDATORY)

1) Behavior mapping:
For each changed endpoint/function, infer and document (internally) expected inputs, outputs, side effects, and error paths from the story AC. Implement and test only those behaviors.

2) Test design:
- API tests assert route contract (status, response schema/body).
- Side effects are tested at unit level on the function that triggers them.
- Do NOT assert side effects from read-only endpoints unless AC explicitly requires it.
- Use `with TestClient(app) as client:` in tests; avoid module-global client creation.

3) External dependencies:
- Mock all external boundaries in tests (HTTP, SDKs, DBs, queues, schedulers, files).
- No real network calls in tests.
- Assert both positive and negative side-effect expectations.

4) Patch safety:
- Preserve existing behavior unless story says otherwise.
- Additive changes preferred (new routers/services/tests).
- If behavior changes, update tests to match explicit AC.

5) Dependency hygiene:
- Add all imported runtime dependencies to requirements.
- Keep tests deterministic and time-safe.

6) Retry triage on pytest failure:
Classify failures as: test expectation mismatch, implementation bug, dependency/config gap, import/path issue, flaky timing.
Fix the smallest correct layer first:
- test mismatch -> fix tests
- behavior mismatch -> fix implementation
- dependency gap -> fix requirements/imports

7) Pre-output self-check:
- Every AC is covered by at least one test.
- No undocumented side-effect assertions.
- No real external calls in tests.
- Output only modified/created files and requirements_add.


"""


def _load_scope() -> dict:
    if not SCOPE_FILE.exists():
        return {"implemented_stories": [], "last_updated": None}
    with open(SCOPE_FILE) as f:
        return json.load(f)


def _save_scope(scope: dict) -> None:
    SCOPE_FILE.parent.mkdir(parents=True, exist_ok=True)
    scope["last_updated"] = datetime.utcnow().isoformat()
    with open(SCOPE_FILE, "w") as f:
        json.dump(scope, f, indent=2)


def _read_existing_codebase() -> str:
    """Read all .py files in backend for context."""
    if not BACKEND.exists():
        return "(empty workspace)"
    lines = []
    for p in sorted(BACKEND.rglob("*.py")):
        if "__pycache__" in str(p):
            continue
        rel = p.relative_to(BACKEND)
        lines.append(f"=== {rel} ===\n{p.read_text(encoding='utf-8')}\n")
    if (BACKEND / "requirements.txt").exists():
        lines.append(f"=== requirements.txt ===\n{(BACKEND / 'requirements.txt').read_text()}\n")
    return "\n".join(lines) if lines else "(empty workspace)"


RETRY_PROMPT = """pytest failed. Fix the code based on this error and output the corrected JSON (files + requirements_add). Do not repeat the error; just fix and output."""
JSON_RETRY_PROMPT = """Your previous response was not valid JSON. Return ONLY valid JSON matching this schema:
{
  "files": [{"path": "relative/path", "content": "full file content"}],
  "requirements_add": ["package"]
}
No markdown fences. No commentary."""

MAX_RETRIES = 2
MAX_JSON_PARSE_RETRIES = 2


def _store_raw_response(raw: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    out = DEBUG_DIR / f"llm_raw_{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}.txt"
    out.write_text(raw, encoding="utf-8")


def _call_llm(client: OpenAI, messages: list[dict]) -> dict:
    parse_errors: list[str] = []
    local_messages = list(messages)

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
            parse_errors.append(str(e))
            _store_raw_response(content)
            local_messages.append({"role": "assistant", "content": content})
            local_messages.append({"role": "user", "content": JSON_RETRY_PROMPT})

    raise ValueError("Invalid JSON from model after retries: " + " | ".join(parse_errors))


def _apply_changes(data: dict) -> None:
    for f in data.get("files", []):
        path = BACKEND / f["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f["content"], encoding="utf-8")
    req_path = BACKEND / "requirements.txt"
    if not req_path.exists():
        req_path.write_text("fastapi\nuvicorn\npytest\nhttpx\n")
    for pkg in data.get("requirements_add", []):
        text = req_path.read_text()
        if pkg not in text:
            req_path.write_text(text.rstrip() + f"\n{pkg}\n")


def _run_pytest() -> tuple[bool, str]:
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"],
        cwd=BACKEND,
        capture_output=True,
        check=False,
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-v", str(BACKEND)],
        capture_output=True,
        text=True,
        cwd=BACKEND,
    )
    if result.returncode != 0:
        return False, f"{result.stderr}\n{result.stdout}"
    return True, ""


def implement_backend(story: Story) -> tuple[bool, str]:
    """Implement story by patching existing codebase. Retries up to MAX_RETRIES on pytest failure."""
    client = OpenAI()
    scope = _load_scope()
    existing = _read_existing_codebase()

    user_content = f"""Existing codebase:
{existing}

---
NEW STORY to implement:
Title: {story.title}
Description: {story.description}
Acceptance criteria: {story.acceptance_criteria}

Extend the codebase. Preserve existing functionality. Output JSON with "files" and "requirements_add"."""

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
            return False, f"LLM JSON parsing failed after {MAX_RETRIES + 1} attempts: {e}"

        _apply_changes(data)

        ok, error_output = _run_pytest()
        if ok:
            implemented = scope.get("implemented_stories", [])
            if story.id not in implemented:
                implemented.append(story.id)
                scope["implemented_stories"] = implemented
            _save_scope(scope)
            retry_note = f" (attempt {attempt + 1})" if attempt > 0 else ""
            return True, f"Patched {BACKEND}. pytest passed{retry_note}. Scope: {scope['implemented_stories']}"

        if attempt < MAX_RETRIES:
            messages.append({"role": "assistant", "content": json.dumps(data)})
            messages.append(
                {
                    "role": "user",
                    "content": f"{RETRY_PROMPT}\n\npytest output:\n{error_output}",
                }
            )
        else:
            return False, f"pytest failed after {MAX_RETRIES + 1} attempts:\n{error_output}"
