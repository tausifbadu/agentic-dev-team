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
  - tests/     where YOU write tests, in api/, ui/, flows/, integration/ subdirectories.

TEST HARNESS (already scaffolded — extend, do NOT rewrite):
  tests/ ships a canonical harness: `conftest.py` (shared fixtures), `pytest.ini`
  (layer markers), `requirements.txt` (pytest/httpx/playwright/pytest-playwright),
  and `factories.py`. Do NOT rewrite conftest.py or pytest.ini — import and REUSE the
  fixtures they provide:
    - `client`     : in-process FastAPI TestClient (context-managed for lifespan) — use for API tests.
    - `api_client` : httpx client bound to a live server — use for flow/integration setup.
    - `base_url`   : live-server URL for UI/E2E (set automatically when the backend is booted).
    - `seed_data`  : parsed tests/seed_data.json (may be empty for now).
    - `page` / `browser` / `context` : Playwright fixtures (from pytest-playwright) for UI/flow tests.
  Only ADD test files and, if you need new test-only deps, append them to
  tests/requirements.txt. Annotate which criterion a test covers with a
  `# covers: <req_id_story_n:ACx>` comment so coverage is attributed.

Workflow:
  1. ORIENT with the cumulative ledger before writing anything, and work in PRIORITY
     ORDER (this run should always move coverage forward, never repeat itself):
       a. `read_test_runs` — FIRST fix any tests FAILING in the latest run (regressions).
       b. `read_test_directives` — then cover the USER-authored flows/edge-cases (highest
          user value); tackle `high` priority first.
       c. `read_test_ledger` — then close the listed GAPS (targets with no test yet).
       d. Only after those, add breadth for any remaining acceptance criteria.
     Then READ the code under test: `list_dir backend`, `read_file backend/main.py`,
     `grep` for routes/models; `read_api_contract` for the route/model summary and
     `read_storypack` for acceptance criteria. Annotate each test for a directive with
     `# directive: <id>` and for an acceptance criterion with `# covers: <req_story:ACx>`
     so coverage is credited and the gap closes.
  2. Write test files across the FOUR layers (import the harness fixtures; do NOT
     rewrite conftest.py). Cover every layer the app supports:
       - tests/api/          API endpoints: status codes, validation, 400/404, enum
                             boundaries. Use the `client` fixture (in-process TestClient).
       - tests/ui/           UI component-presence: key screens/components render their
                             named elements + loading/empty/error states (Playwright `page`).
       - tests/flows/        UI user flows end-to-end: real click-through journeys
                             (create -> see in list -> toggle -> delete), Playwright against
                             the served app (`base_url`).
       - tests/integration/  backend integration flows: multi-endpoint sequences against a
                             live app (create -> GET reflects it -> stats update), via `api_client`.
  3. Validate the API layer with `run_pytest target="tests/api"` — ok=True only if exit==0.
  4. Validate the UI/flow/integration layers with `run_e2e` — it boots the backend, builds
     and serves the frontend (with /api proxied), sets BASE_URL, and runs those suites.
     ok=True only if they pass. (On a host with no browser it soft-skips UI/flow and still
     runs integration.)
  5. Read failures, fix the smallest issue, re-run the relevant validator. Repeat until green.
  6. Call `finish_story(success=true)` ONLY after BOTH run_pytest (API) AND run_e2e
     (when a frontend exists) are green with no edits since.

DUMMY DATA:
  - For request payloads, use the `contract_sample` tool (or import from factories) —
    it returns enum-VALID sample bodies derived from the API contract, so your POSTs
    never 400 on a wrong enum value. Never invent enum strings.
  - A `tests/seed_data.json` (contract-derived, deterministic) is available. For
    populated-list / filter / pagination / board screens, load it and POST it into the
    running app first — `from factories import load_seed, seed_via_api, make`. For
    the create-journey flow, start empty and create through the UI.
  - Tests run against a throwaway DB (the harness isolates it) — never the dev database.

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
  - UI tests use Playwright (Python); test user-visible behavior, no flaky timing.

EFFICIENCY — batch your tool calls. Each turn is an expensive round-trip, so do
as much as is safe per turn: read ALL the code-under-test files you need in ONE
turn (several `read_file` / `grep` at once) rather than one per turn. Only split
across turns when a step DEPENDS on the previous result (e.g. write tests then
run_pytest). Fewer, fuller turns = faster and cheaper.

READ SURGICALLY — to understand the code under test, orient from
`workspace_overview` (export map), use `grep` to locate code, and use
`read_symbol(path, name)` to pull a single function/class rather than reading
whole files. Reserve full `read_file` for when you truly need the entire file."""


class TestAgent(AgentBase):
    agent_id = "test"
    role_description = "Senior QA / Test Engineer"
    workspace_subdir = "tests"
    model_env_var = "TEST_AGENT_MODEL"
    default_model = "codex/gpt-5.5"
    allowed_tools = [
        "read_file", "read_symbol", "write_file", "list_dir", "grep", "apply_patch", "delete_file",
        "run_pytest", "run_e2e",
        "run_python", "http_check", "run_lint", "git_diff",
        "ask_pm", "query_agent", "send_message",
        "read_storypack", "read_past_patterns", "read_api_contract", "contract_sample",
        "read_test_directives", "read_test_ledger", "read_test_runs",
        "finish_story",
    ]
    # Heavier job than a single dev story (four layers + e2e). Larger cap + the
    # existing pacing nudges keep it converging; checkpoint recovery preserves a
    # green-but-capped suite (see run()).
    iteration_cap = 80

    def __init__(self, ctx, on_progress=None):
        super().__init__(ctx, on_progress)
        self.tests_dir = ctx.workspace_dir / "tests"
        self.backend_dir = ctx.workspace_dir / "backend"
        # How this run is recorded in the test ledger: "build" (in-pipeline Phase 3)
        # or "manual" (standalone runner). The standalone runner sets this to "manual".
        self.trigger = "build"

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

        # Ensure the canonical harness scaffold exists in the live tests dir BEFORE we
        # copy it into scratch, so every run reuses the same fixtures/config/deps and
        # the agent only adds test files on top (never rebuilds the harness).
        from agents.test_scaffold import ensure_test_scaffold
        created = ensure_test_scaffold(self.tests_dir)
        if created:
            self._emit("info", f"Test harness scaffold created: {', '.join(created)}")

        # Generate a contract-derived, enum-valid seed dataset if one isn't present.
        # Create-if-missing and honors a manual `_managed` marker — never clobbers a
        # hand-edited seed. Written into the live tests dir so it's copied into scratch
        # (tests load it) and promoted with the suite.
        try:
            from agents.reasoning import load_json
            from agents.test_seed import ensure_seed_data
            contract = load_json(self.ctx.workspace_dir / "contracts" / "api_contract.json", {})
            if contract:
                res = ensure_seed_data(self.tests_dir, contract)
                if res.get("status") in ("created", "regenerated"):
                    self._emit("info",
                               f"Seed data {res['status']} for models: "
                               f"{', '.join(res.get('models', []))}")
        except Exception as exc:  # noqa: BLE001
            self._emit("warn", f"Seed-data generation skipped: {exc}")

        # Test Guidance: ensure the user-directives file exists and mirror any authored
        # directives into the DB so the dashboard + gap analysis see them.
        try:
            from agents.test_directives import (
                ensure_directives_file, load_directives, sync_to_db)
            from agents.test_ledger import project_id_from_workspace
            ensure_directives_file(self.tests_dir)
            directives = load_directives(self.tests_dir)
            if directives:
                sync_to_db(project_id_from_workspace(self.ctx.workspace_dir), directives)
                self._emit("info", f"{len(directives)} user test directive(s) to cover first.")
        except Exception as exc:  # noqa: BLE001
            self._emit("warn", f"Directive sync skipped: {exc}")

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

Write test files across tests/api/, tests/ui/, tests/flows/, tests/integration/ (reuse
the scaffolded harness fixtures — do not rewrite conftest.py). Validate the API layer
with `run_pytest target="tests/api"` and the UI/flow/integration layers with `run_e2e`.
Call `finish_story(success=true)` only once run_pytest AND run_e2e (when a frontend
exists) are green with no edits since; otherwise finish_story(success=false) with the
blocking error."""

        system_prompt = _SYSTEM_BASE
        if TESTING_GUIDELINES:
            system_prompt += f"\n\nTest Engineering Guidelines:\n{TESTING_GUIDELINES}"

        outcome = self._execute_react(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            registry=registry,
            model=self._story_model(story),
        )

        ckpt = None
        if outcome.success:
            # Promote ONLY the tests back — never overwrite the code under test.
            manifest = promote_scratch_copy(scratch / "tests", self.tests_dir, label="test_suite")
            self._log_promotion(manifest)
            self._record_scope(outcome.summary)
            self._emit("info", "Test suite complete; scratch promoted.", outcome.summary)
        else:
            self._emit("error", f"Test suite failed: {outcome.summary}", outcome.error)
            # Preserve a green-but-capped suite instead of discarding it (#4).
            ckpt = self._preserve_checkpoint(
                scratch=scratch / "tests", target_dir=self.tests_dir,
                registry=registry, story=story,
            )

        # Record this run into the per-POC test ledger (coverage + history). Reads the
        # JUnit reports accumulated by run_pytest and scans the scratch tests dir (what
        # was actually executed). Best-effort — must never break the run.
        try:
            from agents.test_ledger import record_test_run
            junit = list((registry.context.metadata or {}).get("junit_reports") or [])
            record_test_run(
                workspace_dir=self.ctx.workspace_dir,
                run_id=self.ctx.run_id,
                storypack_id=self.ctx.storypack_id,
                trigger=getattr(self, "trigger", "build"),
                junit_paths=junit,
                stories=self.ctx.all_stories,
                status=("passed" if outcome.success else "failed"),
                summary=(outcome.summary or outcome.final_text or "")[:500],
                tests_source=scratch / "tests",
            )
            self._emit("info", "Test ledger updated (coverage + run history).")
        except Exception as exc:  # noqa: BLE001
            self._emit("warn", f"Test ledger recording failed: {exc}")

        meta = dict(outcome.metadata or {})
        if ckpt:
            meta["recoverable_checkpoint"] = ckpt
        return RunResult(
            success=outcome.success,
            summary=outcome.summary or outcome.final_text,
            iterations=outcome.iterations,
            tool_calls=registry.context.tool_call_count,
            metadata=meta,
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
