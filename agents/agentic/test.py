"""Test Agent — class-based ReAct runtime."""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agents.agentic.base import AgentBase, RunResult
from agents.reasoning import (
    load_json,
    load_skill_guidelines,
    promote_scratch_copy,
    save_json,
    update_scope,
    workspace_file_tree,
)
from schemas import Story

SKILL_TESTING_PATH = (
    Path(__file__).parent.parent.parent / ".cursor" / "skills" / "agentic-dev-team" / "SKILL_TESTING.md"
)
TESTING_GUIDELINES = load_skill_guidelines(SKILL_TESTING_PATH)

_TEST_COPY_IGNORE = shutil.ignore_patterns(
    "node_modules", ".venv", "__pycache__", "dist", "build", ".git", ".pytest_cache"
)


_SYSTEM_BASE = """You are the Test Agent — a Senior Test Engineer who writes pytest API tests,
Playwright UI tests, and integration tests.

Your working copy (scratch root) contains:
  - backend/   the FastAPI code UNDER TEST — READ it, never modify it.
  - frontend/  the React code under test (present only if a frontend was built).
  - tests/     where YOU write tests, in api/, ui/, integration/ subdirectories.

Workflow:
  1. READ the code under test first: `list_dir backend`, `read_file backend/main.py`,
     `grep` for routes/models. Use `read_api_contract` for the route/model summary,
     `read_storypack` for the acceptance criteria you must cover.
  2. Write test files under `tests/api/`, `tests/ui/`, `tests/integration/` with
     `write_file` or `apply_patch`.
  3. Validate with `run_pytest target="tests"` — ok=True only if exit==0.
  4. Read failures, fix the smallest issue, re-run. Repeat until green.
  5. Call `finish_story(success=true)` once the suite passes.

Import convention (CRITICAL — get this right or every test errors on import):
  - During `run_pytest`, the backend directory is ON sys.path. Import the app and
    modules DIRECTLY, exactly as the backend does internally:
        from fastapi.testclient import TestClient
        from main import app
        from models import CustomerCreate
    Do NOT prefix imports with "backend." — there is no `backend` package.
  - Use TestClient AS A CONTEXT MANAGER so FastAPI lifespan startup runs and
    app.state is initialized (otherwise routes that read app.state raise
    AttributeError):
        with TestClient(app) as client:
            r = client.get("/api/customers")
  - NEVER make real network calls. Mock external services (geopy/Nominatim, HTTP
    clients) by patching them where they are USED (e.g. the geocoding service the
    app imports). Use FastAPI dependency_overrides or monkeypatch.
  - Use an isolated temporary SQLite database so tests never touch the dev DB.

Hard constraints:
  - You MUST finish by calling `finish_story`. A plain text reply does nothing. If
    you genuinely cannot make tests pass, call finish_story(success=false) with the
    exact blocking error.
  - PRESERVE existing test files — only add or extend.
  - Add any new test-only packages (e.g. httpx, playwright) to
    `tests/requirements.txt`.
  - UI tests use Playwright (Python); test user-visible behavior, no flaky timing."""


class TestAgent(AgentBase):
    agent_id = "test"
    role_description = "Senior QA / Test Engineer"
    workspace_subdir = "tests"
    model_env_var = "TEST_AGENT_MODEL"
    default_model = "gpt-4o-mini"
    allowed_tools = [
        "read_file", "write_file", "list_dir", "grep", "apply_patch", "delete_file",
        "run_pytest",
        "run_python", "http_check", "git_diff",
        "ask_pm", "query_agent", "send_message",
        "read_storypack", "read_past_patterns", "read_logs", "read_api_contract",
        "finish_story",
    ]
    iteration_cap = 50

    def __init__(self, ctx, on_progress=None):
        super().__init__(ctx, on_progress)
        self.tests_dir = ctx.workspace_dir / "tests"
        self.backend_dir = ctx.workspace_dir / "backend"

    def run(self, story: Optional[Story] = None) -> RunResult:
        """For tests, the 'story' argument is optional — we test the full pack.

        The scratch root is a *combined* working copy so the agent can both SEE the
        code under test (via file tools) and IMPORT it (via run_pytest's PYTHONPATH):

            <scratch>/backend/   copy of the code under test (read-only by convention)
            <scratch>/frontend/  copy of the frontend, when present
            <scratch>/tests/     existing tests (or empty) — the agent writes here

        Only ``tests/`` is promoted back to the workspace on success, so the test
        agent can never clobber the backend/frontend it is testing.
        """
        self._current_story = story
        self._emit("info", "Test agent designing test suite for storypack")

        ws = self.ctx.workspace_dir
        scratch = Path(tempfile.mkdtemp(prefix="tests_attempt_"))
        for sub in ("backend", "frontend"):
            src = ws / sub
            if src.exists():
                try:
                    shutil.copytree(src, scratch / sub, ignore=_TEST_COPY_IGNORE, dirs_exist_ok=True)
                except OSError:
                    pass
        if self.tests_dir.exists():
            try:
                shutil.copytree(self.tests_dir, scratch / "tests", ignore=_TEST_COPY_IGNORE, dirs_exist_ok=True)
            except OSError:
                (scratch / "tests").mkdir(parents=True, exist_ok=True)
        else:
            (scratch / "tests").mkdir(parents=True, exist_ok=True)

        registry = self._build_registry(story, scratch)
        registry.context.metadata["backend_dir"] = str(scratch / "backend")

        contract_section = ""
        contract_path = ws / "contracts" / "api_contract.json"
        if contract_path.exists():
            contract_section = (
                "\n\nLatest backend API contract:\n"
                + json.dumps(load_json(contract_path, {}), indent=2)[:3500]
            )

        backend_tree = workspace_file_tree(scratch / "backend", {".py"})

        story_summaries = [
            {"id": s.id, "title": s.title, "ownership": s.ownership,
             "acceptance_criteria": s.acceptance_criteria,
             "test_focus": s.test_focus}
            for s in self.ctx.all_stories
        ]

        user_prompt = f"""Storypack to test:
{json.dumps(story_summaries, indent=2)}

Original requirement (truncated):
{self.ctx.requirement_text[:2000]}{contract_section}

Backend modules under test (import these DIRECTLY, e.g. `from main import app`):
{backend_tree}

Write test files under tests/api/, tests/ui/, tests/integration/. After writing,
run `run_pytest target="tests"`. Call `finish_story(success=true)` once it passes,
or finish_story(success=false) with the blocking error if you cannot."""

        system_prompt = _SYSTEM_BASE
        if TESTING_GUIDELINES:
            system_prompt += f"\n\nTest Engineering Guidelines:\n{TESTING_GUIDELINES}"

        outcome = self._execute_react(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            registry=registry,
        )

        if outcome.success:
            # Promote ONLY the tests back — never overwrite the code under test.
            promote_scratch_copy(scratch / "tests", self.tests_dir)
            self._record_scope(outcome.summary)
            self._emit("info", "Test suite complete; scratch promoted.", outcome.summary)
        else:
            self._emit("error", f"Test suite failed: {outcome.summary}", outcome.error)

        return RunResult(
            success=outcome.success,
            summary=outcome.summary or outcome.final_text,
            iterations=outcome.iterations,
            tool_calls=registry.context.tool_call_count,
            metadata=outcome.metadata,
        )

    # ---------- Helpers ----------

    def _record_scope(self, summary: str) -> None:
        scope_file = self.ctx.workspace_dir / "scope.json"
        scope = update_scope(scope_file, {
            "id": "test_suite",
            "title": "Test Suite",
            "owner": "testing",
            "summary": summary[:300],
            "applied_at": datetime.now(timezone.utc).isoformat(),
        })
        save_json(scope_file, scope)
