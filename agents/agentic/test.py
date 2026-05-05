"""Test Agent — class-based ReAct runtime."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agents.agentic.base import AgentBase, RunResult
from agents.reasoning import (
    create_scratch_copy,
    load_json,
    promote_scratch_copy,
    save_json,
    update_scope,
)
from schemas import Story


_SYSTEM_BASE = """You are the Test Agent — a Senior Test Engineer who writes pytest API tests,
Playwright UI tests, and integration tests.

Your assignment: design and validate a test suite for the entire storypack inside an
isolated scratch directory.

Workflow:
  1. Read the storypack and existing code (`read_storypack`, `list_dir`, `read_file`).
  2. Read the published API contract via `read_api_contract` if present.
  3. Create test files under `api/`, `ui/`, and `integration/` subdirectories using
     `apply_patch` or `write_file`. Always import from the actual existing modules
     (consult the file listing).
  4. Validate by calling `run_pytest` (it picks up the api/ subdirectory by default).
     For UI/integration suites, write tests that mock browser/HTTP calls.
  5. Call `finish_story` once the suite is generated and api tests pass.

Hard constraints:
  - API tests use pytest + httpx + FastAPI TestClient. NEVER make real network calls.
  - UI tests use Playwright (Python). Test user-visible behavior only.
  - Mock external services. No flaky timing.
  - PRESERVE existing test files when adding new ones — only add or extend.
  - Add any new test packages to the local `requirements.txt`."""


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
        """For tests, the 'story' argument is optional — we test the full pack."""
        self._current_story = story
        self._emit("info", "Test agent designing test suite for storypack")

        scratch = create_scratch_copy(self.tests_dir, "tests_attempt")

        # Mirror backend into scratch's sibling so api/integration tests can import.
        if self.backend_dir.exists():
            backend_mirror = scratch.parent / "backend"
            if not backend_mirror.exists():
                try:
                    shutil.copytree(self.backend_dir, backend_mirror)
                except Exception:
                    pass

        registry = self._build_registry(story, scratch)

        contract_section = ""
        contract_path = self.ctx.workspace_dir / "contracts" / "api_contract.json"
        if contract_path.exists():
            contract_section = (
                "\n\nLatest backend API contract:\n"
                + json.dumps(load_json(contract_path, {}), indent=2)[:3500]
            )

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

Generate test files under api/, ui/, integration/ subdirectories.
After writing, run `run_pytest target="api"` (and any other suite you can validate
without a running backend). Call `finish_story` once api tests pass."""

        outcome = self._execute_react(
            system_prompt=_SYSTEM_BASE,
            user_prompt=user_prompt,
            registry=registry,
        )

        if outcome.success:
            promote_scratch_copy(scratch, self.tests_dir)
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
