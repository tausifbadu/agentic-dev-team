"""Test Agent. Generates and runs API, UI, and integration tests."""

import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
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
FRONTEND = WORKSPACE / "frontend"
TESTS_DIR = WORKSPACE / "tests"
SCOPE_FILE = WORKSPACE / "scope.json"
DEBUG_DIR = WORKSPACE / "debug"

MODEL_NAME = os.getenv("TEST_AGENT_MODEL", "gpt-4o-mini")
MAX_RETRIES = 2
MAX_JSON_PARSE_RETRIES = 2

PLANNER_PROMPT = """You are a test planning engineer. Analyze the existing backend and frontend code and produce a comprehensive test plan.

Return JSON only:
{
  "summary": "short test plan summary",
  "api_tests": [{"target": "endpoint or module", "cases": ["test case description"]}],
  "ui_tests": [{"target": "page or component", "cases": ["test case description"]}],
  "integration_tests": [{"flow": "end-to-end flow", "steps": ["step description"]}],
  "risk_areas": ["areas most likely to have bugs"]
}

Rules:
- API tests use pytest + httpx with FastAPI TestClient. Mock all external calls.
- UI tests use Playwright for browser-based testing of the React frontend.
- Integration tests verify frontend-to-backend flows with a running backend.
- Prioritize tests that cover acceptance criteria from the stories.
- Include edge cases: invalid input, empty responses, error states, loading states.
- Do not test implementation details; test observable behavior.
"""

CODER_PROMPT = """You are a Test Engineer. Generate test files based on the provided test plan.

Hard constraints:
- API tests: use pytest + httpx + FastAPI TestClient. Mock external dependencies.
- UI tests: use Playwright (Python). Test user-visible behavior, not internals.
- Integration tests: use pytest + httpx. Start backend via subprocess if needed.
- All tests must be runnable independently.
- No real network calls in any test.
- Output JSON only:
{
  "files": [{"path": "relative/path/from/tests", "content": "full file content"}],
  "requirements_add": ["package1"],
  "npm_dependencies_add": ["package1"]
}
"""

JSON_RETRY_PROMPT = """Your previous response was not valid JSON. Return ONLY valid JSON matching the requested schema. No markdown fences. No commentary."""
TEST_RETRY_PROMPT = """Tests failed. Classify whether the issue is in the test code, test setup, or missing dependencies. Fix the smallest correct layer and return corrected JSON only."""


def _store_raw_response(raw: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out = DEBUG_DIR / f"test_raw_{ts}_{uuid.uuid4().hex[:8]}.txt"
    out.write_text(raw, encoding="utf-8")


def _call_llm_json(client: OpenAI, prompt: str, system_prompt: str) -> dict:
    from agents.llm_client import call_llm_json
    return call_llm_json(
        client, MODEL_NAME, system_prompt, prompt,
        max_retries=MAX_JSON_PARSE_RETRIES,
        retry_prompt=JSON_RETRY_PROMPT,
        on_raw_response=_store_raw_response,
    )


def _gather_workspace_context() -> str:
    """Read both backend and frontend source for comprehensive test generation."""
    sections: list[str] = []

    if BACKEND.exists():
        be_files = select_relevant_files(BACKEND, "api endpoint test route model", {".py"}, max_files=12)
        if be_files:
            sections.append("=== BACKEND CODE ===")
            sections.append(render_context(BACKEND, be_files))

    if FRONTEND.exists():
        fe_files = select_relevant_files(
            FRONTEND, "component page app fetch api", {".js", ".jsx", ".ts", ".tsx", ".css", ".html"}, max_files=10
        )
        if fe_files:
            sections.append("=== FRONTEND CODE ===")
            sections.append(render_context(FRONTEND, fe_files))

    return "\n\n".join(sections) if sections else "(empty workspace)"


CONTRACTS_DIR = WORKSPACE / "contracts"


def _load_api_contract() -> dict | None:
    path = CONTRACTS_DIR / "api_contract.json"
    if path.exists():
        return load_json(path, {})
    return None


def _stories_context(all_stories: list[Story], requirement_text: str) -> str:
    story_data = [
        {
            "id": s.id,
            "title": s.title,
            "ownership": s.ownership,
            "acceptance_criteria": s.acceptance_criteria,
            "implementation_notes": s.implementation_notes,
            "test_focus": s.test_focus,
        }
        for s in all_stories
    ]
    bundle: dict = {
        "requirement": requirement_text,
        "stories": story_data,
        "implemented_scope": load_json(SCOPE_FILE, {"implemented_stories": []}),
    }
    contract = _load_api_contract()
    if contract:
        bundle["api_contract"] = contract
    return json.dumps(bundle, indent=2)


def _plan_tests(client: OpenAI, all_stories: list[Story], requirement_text: str) -> dict:
    prompt = f"""Test planning payload:
{_stories_context(all_stories, requirement_text)}

Workspace code:
{_gather_workspace_context()}
"""
    return _call_llm_json(client, prompt, PLANNER_PROMPT)


def _generate_test_patch(
    client: OpenAI,
    all_stories: list[Story],
    requirement_text: str,
    plan: dict,
    error_output: str = "",
) -> dict:
    retry_block = f"\nPrevious test failure:\n{error_output}\n" if error_output else ""
    prompt = f"""Test implementation payload:
{_stories_context(all_stories, requirement_text)}

Approved test plan:
{json.dumps(plan, indent=2)}

Workspace code:
{_gather_workspace_context()}
{retry_block}
Generate test files for api/, ui/, and integration/ subdirectories.
"""
    if error_output:
        return _call_llm_json(client, prompt + "\n" + TEST_RETRY_PROMPT, CODER_PROMPT)
    return _call_llm_json(client, prompt, CODER_PROMPT)


def _apply_changes(data: dict, root: Path) -> None:
    for file_change in data.get("files", []):
        path = root / file_change["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(file_change["content"], encoding="utf-8")

    req_path = root / "requirements.txt"
    base_deps = "pytest\nhttpx\nplaywright\npytest-playwright\nfastapi\nuvicorn\n"
    if not req_path.exists():
        req_path.write_text(base_deps, encoding="utf-8")
    existing = req_path.read_text(encoding="utf-8")
    for package in data.get("requirements_add", []):
        if package not in existing:
            existing = existing.rstrip() + f"\n{package}\n"
    req_path.write_text(existing, encoding="utf-8")


def _run_api_tests(tests_root: Path) -> tuple[bool, str]:
    """Run pytest on the api/ subdirectory."""
    api_dir = tests_root / "api"
    if not api_dir.exists() or not list(api_dir.glob("test_*.py")):
        return True, "No API tests to run."

    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"],
        cwd=tests_root,
        capture_output=True,
        check=False,
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-v", str(api_dir)],
        cwd=tests_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 5):
        return False, f"API tests failed:\n{result.stderr}\n{result.stdout}"
    return True, result.stdout


def _run_ui_tests(tests_root: Path) -> tuple[bool, str]:
    """Run Playwright tests from the ui/ subdirectory."""
    ui_dir = tests_root / "ui"
    if not ui_dir.exists() or not list(ui_dir.glob("test_*.py")):
        return True, "No UI tests to run."

    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        cwd=tests_root,
        capture_output=True,
        check=False,
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-v", str(ui_dir)],
        cwd=tests_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 5):
        return False, f"UI tests failed:\n{result.stderr}\n{result.stdout}"
    return True, result.stdout


def _run_integration_tests(tests_root: Path) -> tuple[bool, str]:
    """Run integration tests from the integration/ subdirectory."""
    int_dir = tests_root / "integration"
    if not int_dir.exists() or not list(int_dir.glob("test_*.py")):
        return True, "No integration tests to run."

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-v", str(int_dir)],
        cwd=tests_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 5):
        return False, f"Integration tests failed:\n{result.stderr}\n{result.stdout}"
    return True, result.stdout


def _run_all_tests(tests_root: Path) -> tuple[bool, str]:
    """Run all test suites and collect results."""
    results: list[str] = []
    all_passed = True

    for name, runner in [("API", _run_api_tests), ("UI", _run_ui_tests), ("Integration", _run_integration_tests)]:
        ok, output = runner(tests_root)
        results.append(f"--- {name} Tests ---\n{output}")
        if not ok:
            all_passed = False

    combined = "\n\n".join(results)
    return all_passed, combined


def implement_tests(
    all_stories: list[Story],
    requirement_text: str = "",
    on_progress: ProgressCallback = None,
    fix_context: str = "",
) -> tuple[bool, str]:
    """Plan, generate, validate tests in scratch, promote on success."""
    client = OpenAI(timeout=90)
    last_error = fix_context
    emit = on_progress or (lambda msg, detail=None: None)

    if fix_context:
        emit("Fix mode: injecting error context from previous failure")

    for attempt in range(MAX_RETRIES + 1):
        emit(f"Planning test suite (attempt {attempt + 1})...")
        try:
            plan = _plan_tests(client, all_stories, requirement_text)
            emit("Test plan ready", json.dumps(plan, indent=2))

            emit("Generating test files...")
            patch = _generate_test_patch(client, all_stories, requirement_text, plan, last_error)
            file_paths = [f.get("path", "?") for f in patch.get("files", [])]
            emit(f"Tests generated: {', '.join(file_paths)}", _format_files_detail(patch))
        except ValueError as exc:
            return False, str(exc)

        emit("Running test suites...")
        scratch = create_scratch_copy(TESTS_DIR, "tests_attempt")
        _apply_changes(patch, scratch)

        if BACKEND.exists():
            backend_in_scratch = scratch.parent / "backend"
            if not backend_in_scratch.exists():
                shutil.copytree(BACKEND, backend_in_scratch)

        ok, output = _run_all_tests(scratch)
        if ok:
            emit("All tests passed", output)
            promote_scratch_copy(scratch, TESTS_DIR)
            scope = update_scope(
                SCOPE_FILE,
                {
                    "id": "test_suite",
                    "title": "Test Suite",
                    "owner": "testing",
                    "summary": plan.get("summary", "Test suite generated"),
                    "applied_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            save_json(SCOPE_FILE, scope)
            retry_note = f" (attempt {attempt + 1})" if attempt > 0 else ""
            return True, f"Tests generated at {TESTS_DIR}{retry_note}.\n{output}"
        emit("Tests failed, retrying...", output)
        last_error = output

    return False, f"Tests failed after {MAX_RETRIES + 1} attempts:\n{last_error}"
