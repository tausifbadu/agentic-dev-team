# Plan — Strengthened Test Agent + Per-Workspace Test Coverage Monitoring

Status: **Approved for implementation** · Target branch: `sandbox`

## Objective

Strengthen the Test Agent so that, given full context of a POC workspace, it authors
and runs scripts across four layers — **API**, **UI components**, **UI user flows**, and
**backend integration flows** — using realistic, editable dummy data. Every test run for
a POC is **recorded per workspace** into a durable ledger the agent reads on its next
execution (to fill gaps and re-run regressions), and a dedicated **dashboard page** lets
you monitor exactly what has been tested for each workspace folder.

## Locked design decisions

| Decision | Choice |
|---|---|
| Ledger storage | **Workspace file (canonical) + SQLite mirror (fast UI queries)** |
| Run trigger | **Standalone "Run tests" action** (on-demand, no rebuild) **+** existing in-build Phase 3 |
| UI test tooling | **Playwright (Python) only** — reuse the existing `check_ui` boot/serve/proxy machinery |
| Dummy data | **Persistent, editable, contract-validated seed** + ephemeral self-seeding for API/flow/integration |
| Full context to agent | Env-gated (`TEST_AGENT_FULL_CONTEXT=1`) — affordable since the test phase runs once, not per ReAct turn |

## Guiding principles

- **Per-POC scoping** — everything keys on `project_id` (`workspace_paths.resolve_workspace_dir`).
- **Canonical file, fast mirror** — ledger is JSON in the workspace; SQLite mirrors it for the UI.
- **Trust results, not claims** — coverage/gaps computed from machine-readable JUnit XML + static
  contract/frontend analysis, never from what the agent asserts it did.
- **One recording path** — the standalone run and the build Phase 3 write through the same recorder.
- **User data is authoritative** — the agent never silently clobbers a hand-edited seed set.

---

## 1. The test ledger (data model)

### A. Workspace files — `workspace/projects/<slug>/tests/`

**`test_manifest.json`** — coverage source of truth:

```jsonc
{
  "project_id": "helpdesk",
  "updated_at": "...",
  "contract_hash": "<sha256 of api_contract.json>",   // detects contract drift since last run
  "coverage": {
    "api":          [{ "target": "POST /api/tickets", "test_file": "tests/api/test_tickets.py",
                       "test_names": ["test_create_valid","test_create_400"],
                       "acceptance_refs": ["req_x_story_2:AC1"],
                       "last_status": "passed", "last_run_id": "...", "last_run_at": "..." }],
    "ui_component": [{ "target": "TicketTable", "...": "..." }],
    "ui_flow":      [{ "target": "Create ticket end-to-end", "...": "..." }],
    "integration":  [{ "target": "Create -> appears in list (BE+FE)", "...": "..." }]
  },
  "gaps": [{ "kind": "api", "target": "DELETE /api/tickets/{id}", "reason": "no test references this endpoint" }],
  "totals": { "api": {"covered": 6, "total": 8, "passed": 5, "failed": 1} }
}
```

**`runs/run_<run_id>.json`** — full per-run detail (per-case results + raw pytest/playwright output excerpts).

**`seed_data.json`**, **`factories.py`**, **`conftest.py`** — the dummy-data artifacts (see §5).

All promoted alongside test code (extend `TestAgent.run` promote so `tests/` includes these).

### B. SQLite mirror — `state_store.py` (new tables, keyed by `project_id`, idempotent ALTER migrations)

- **`test_run`** — one row per run: `id, project_id, storypack_id, run_id, trigger ('build'|'manual'), status, totals_json, summary, started_at, finished_at`.
- **`test_case_result`** — per case per run: `test_run_id (FK), project_id, layer ('api'|'ui_component'|'ui_flow'|'integration'), target, test_file, test_name, status, duration_ms, message, acceptance_refs`.
- **`test_coverage`** — current-state upsert keyed `(project_id, layer, target)`: last status/run/test_file/acceptance_refs + `is_gap`. Lets the UI render current coverage without parsing files.
- **`seed_edit`** (optional) — audit of seed-data edits (who/when/what), mirroring `workspace_chat_edits`.
- **`test_directive`** — user-authored test intent (see §6): `id, project_id, kind, title, steps_json, expected_outcome, priority, status, linked_test_ids_json, created_by, updated_at`. Mirrors the canonical `tests/test_directives.json`.

The existing thin `test_results` table stays for backward compat; new tables supersede it.

---

## 2. Strengthen the Test Agent (`agents/agentic/test.py`)

### A. Full context (gated by `TEST_AGENT_FULL_CONTEXT=1`)

- Prior **ledger**: current coverage, last statuses, and **gaps** — so the agent extends cumulatively,
  prioritizing gaps and regressions instead of starting from zero.
- **Full** acceptance criteria + `test_focus` for every story, cross-linked to coverage targets.
- `api_contract.json` incl. **enum values** (test valid + invalid enum inputs -> the 400 paths).
- Backend export map **and** frontend file tree/export map (component names for `ui_component` targets)
  — today the test agent only receives the backend tree.
- **User test directives** (see §6): business/process/data flows and edge cases you authored — injected as
  **highest-priority context** ("cover these first, before auto-detected gaps").

### B. Four explicit test layers

| Dir | Layer | What it validates | Tooling |
|---|---|---|---|
| `tests/api/` | API | status codes, validation (valid + 400/404), enum boundaries | pytest + httpx/TestClient |
| `tests/ui/` | UI component | each key screen/component renders named elements + loading/empty/error states | Playwright vs built `dist/` |
| `tests/flows/` | UI user flow | click-through journeys (create -> see in list -> toggle -> delete) w/ backend booted, `/api` proxied | Playwright |
| `tests/integration/` | backend integration | multi-endpoint sequences against a live app (create -> GET reflects -> stats update) | pytest + httpx |

### C. New / adjusted tools (add to `TestAgent.allowed_tools`)

- `read_test_ledger()` — manifest coverage + gaps + last-run summary (new in `state_tools.py`).
- `read_test_directives()` — user-authored flows/edge-cases to cover first, with their current status (new in `state_tools.py`).
- `read_test_runs(limit)` — recent run headers + failing cases (regression awareness).
- `contract_sample(model)` — return a **valid** dummy instance from `api_contract.json` field types +
  `enum_values` (dummy data always uses accepted vocabulary; reuses the enum-drift machinery).
- `run_e2e()` — **key new exec tool** (`exec_tools.py`): boots the backend + serves the frontend `dist/`
  with `/api` reverse-proxy (reuse `check_ui`'s `_boot_backend_for_ui` + `_serve_dir`), exports `BASE_URL`,
  then runs pytest over `tests/ui` + `tests/flows` + `tests/integration` with Playwright headless.
  Emits **JUnit XML** (`--junitxml`). Installs the Playwright browser if missing (soft-skip like `check_ui`,
  clearly flagged).
- Keep `run_pytest` for the API layer (also emit JUnit XML).

### D. Definition-of-Done gate for `testing` (`agent_tools.py`)

Add `testing` to the required-validator map (today it's skipped):
- Require a recent green `run_pytest` (API) **and**, when a frontend exists, a green `run_e2e`,
  with **no edits since** (existing `last_validation` / `dirty_since_validation` mechanism).
- `finish_story(success=false)` still escapes to the supervisor's skip/simplify/halt path.

---

## 3. Recording every run (`agents/test_ledger.py`, new)

`record_test_run(project_id, run_id, storypack_id, trigger, junit_paths, workspace_dir, stories, contract)`:

1. Parse **JUnit XML** from `run_pytest` + `run_e2e` -> per-case `{layer, test_file, test_name, status, duration, message}`
   (layer inferred from path: `api/` / `ui/` / `flows/` / `integration/`).
2. **Map cases -> targets**: statically scan test files for endpoint path strings (intersect with contract routes)
   and component names (intersect with FE export map); attach `acceptance_refs` and `directive` ids from an agent
   annotation convention (e.g. `# covers: req_x_story_2:AC1` / `# directive: dir_7a3f`).
3. **Compute gaps**: contract endpoints with no api test; FE components/screens with no ui test;
   acceptance criteria / user journeys with no flow test; **and any user directive with no linked passing test**
   (uncovered directives, especially high-priority, are surfaced as gaps).
   Update each directive's `status` (uncovered/covered/passed/failed) + `linked_test_ids`.
4. Write `test_manifest.json` (+ recompute `totals`, `contract_hash`) and `runs/run_<id>.json`;
   **mirror** into `test_run` / `test_case_result` / `test_coverage`.

Called from **both** the standalone runner and Supervisor Phase 3 -> uniform history.

---

## 4. Standalone "Run tests" capability

- **`agents/test_runner.py`** (new, thin): build a minimal `RunContext` (bus + budget) for a project,
  run `TestAgent` against a synthetic "test the whole POC" story, then call `record_test_run`.
  Refactor Supervisor Phase 3 to call this same runner.
- **`dashboard/backend/execution.py`**: `run_tests_background(project_id, scope?)` on a daemon thread;
  single-flight `test_run_state` (409 on concurrent, mirroring `fix_state` / `enhance_state`).
- **CLI**: `python main.py --test <project_id>` for parity.

---

## 5. Dummy data — persistent, editable, contract-validated

### Philosophy
Deterministic, isolated, contract-derived, reproducible across runs.

### Isolation — never touch the dev DB
Each run gets a throwaway SQLite, auto-detected:
- **Temp-DB override** when the backend reads its DB path from env/config -> boot against a fresh temp file.
- **Self-seed on empty** fallback when the backend hardcodes its path -> start clean, seed through the API.
- (Nudge the backend agent's skill to make the DB path env-configurable -> every POC becomes test-friendlier.)

### Layered use of data
- **API tests** — self-seed via the API (POST sample -> GET/PATCH/DELETE). Deterministic; also tests the write path.
- **Integration flows** — create -> read-reflects-it -> update -> stats-update, self-contained per test.
- **UI flows (E2E)** — start empty and test the **create journey** (create -> appears -> toggle -> delete).
- **UI components / populated screens** — load the **seed dataset** first (list/filter/pagination/empty-vs-populated).

### Persistent editable seed (`tests/seed_data.json` + `factories.py` + `conftest.py`)
- Agent generates the initial set from the contract (types + `enum_values`) with a **fixed random seed**.
- Realistic spread (e.g. N rows across every status/priority/category).
- Reused every run -> reproducible, comparable history; doubles as **demo data** ("Seed live app").

### Editability rules (user data is authoritative)
- Editable **by hand**, **in the dashboard** (seed panel), or **regenerated by the agent**.
- If `seed_data.json` exists, the agent treats it as source of truth — it only **fills gaps**
  (adds rows for an entity/enum value with zero coverage), never overwrites your rows.
- Overwriting is **explicit**: a **"Regenerate from contract"** action backs up the old file first
  (`.backups/` pattern) — reversible.
- Optional `"_managed": "manual"` marker fully locks the file from any agent touch.

### Guardrails
- **On Save, validate against `api_contract.json`** (field types + `enum_values`, reusing contract-lint):
  invalid values (e.g. `"general"` vs `"General"`) are flagged before they cause a 400.
- Determinism preserved — auto-generated rows use the fixed seed; manual rows kept verbatim.
- Edits recorded in `seed_edit` (like `workspace_chat_edits`).

---

## 6. Test Guidance — user-authored directives

A **human test-intent layer** on top of what the agent auto-derives: business flows, process flows, data
flows, edge cases, negative cases, and cross-story journeys. Distinct from acceptance criteria (those are
per-story, from the PM) — directives are *your* QA intent, often spanning multiple stories/endpoints.

### Canonical file — `workspace/projects/<slug>/tests/test_directives.json` (+ `test_directive` DB mirror)

```jsonc
{
  "id": "dir_7a3f",
  "kind": "business_flow",   // business_flow | process_flow | data_flow | integration_flow
                             //   | edge_case | negative_case | ui | api | note
  "title": "New customer -> geocoded -> appears on map",
  "steps": ["POST /api/customers with address", "geocoding resolves lat/lng",
            "GET /api/customers returns it", "UI map shows the pin"],
  "expected_outcome": "Customer persisted with coords; visible in list + map",
  "priority": "high",
  "status": "uncovered",     // auto-maintained by the recorder: uncovered | covered | passed | failed
  "linked_test_ids": [],     // filled by the recorder from `# directive: dir_7a3f` annotations
  "created_by": "user", "updated_at": "..."
}
```

### Agent integration
- Read via `read_test_directives()` as **highest-priority context** — "cover these before auto-detected gaps."
- Directives become **first-class coverage targets** in the manifest (a `directives` section) and feed **gap
  analysis** (§3); priority ordering ties into the loop budget so user flows are covered first.
- The recorder maps authored tests -> directives via the `# directive: <id>` annotation and sets each
  directive's status from JUnit results.

### Ownership rules (same as seed data)
- User-authored directives are **authoritative**: the agent never edits/deletes them — it only links tests and
  reports status.
- Editable by hand or in the dashboard **Test Guidance** tab (§9).

### The three inputs that drive coverage
1. **Auto-detected** — from contract routes + PM stories/acceptance criteria.
2. **Seed data** (§5) — your editable dataset.
3. **Test directives** (this section) — your editable flow/edge-case intent.

Gaps = anything across all three without a passing test.

---

## 7. Test Harness (canonical, reused scaffold)

The tests can't run without fixtures/config; the question is *explicit-and-reused* vs *reinvented-each-run*.
Because the feature is **cumulative** (compare runs over time), the harness must be **stable** — so we ship a
canonical scaffold (like the frontend's `_ensure_project_scaffold`) dropped into `tests/` on first run, and the
agent only **adds test files** on top. This guarantees determinism, saves iterations against the no-cache
gateway, and sidesteps the import/lifespan/DB mistakes the current skill has to warn against.

### Scaffolded artifacts (promoted + reused every run; agent extends, never rebuilds)

```
tests/
  conftest.py        # fixtures: temp SQLite DB, TestClient (context-managed for lifespan),
                     #   httpx client, playwright browser/context/page, base_url,
                     #   seed loader (from seed_data.json), cleanup/teardown
  pytest.ini         # markers: api / ui / flow / integration; --junitxml path;
                     #   log level; (later) trace/screenshot on failure
  requirements.txt   # pytest, httpx, playwright, pytest-playwright
  factories.py       # data builders (from §5)
  seed_data.json     # editable seed (from §5)
```

### Rules
- **Minimal by design.** v1 = fixtures + config + deps only. Trace/screenshot artifacts, retries, and a
  session-scoped server-lifecycle fixture are **later polish**, not v1.
- **Env contract** documented once: `BASE_URL`, API base, temp DB path, ports — how tests discover the booted app.
- Regeneratable but **user-editable/lockable** (same `_managed` marker + backup-on-regenerate as the seed).
- Promoted via the manifest/backup path (§8) so a scaffold change is attributable and reversible.

---

## 8. Harness & Loop Engineering (reuse the engine; tune the knobs)

**No new reasoning core, no new orchestration engine.** The test agent already runs the shared ReAct loop
(`react_loop.py` + `call_llm_with_tools`) as an `AgentBase`, and the Supervisor already orchestrates it
(Phase 3). Strengthening it needs only **test-specific tuning of the existing loop machinery** — because we are
~tripling the agent's job (4 layers + e2e + seed + directives).

- **Iteration cap + pacing** — raise `TestAgent.iteration_cap` (50 -> ~80); treat each layer as an internal
  milestone. Existing 70%/final-3 pacing nudges apply unchanged.
- **Budget model** — account `run_e2e`'s wall-clock (server boot + browser install + Playwright) in the run
  budget; run `run_e2e` **once per milestone**, not per edit (mirror `http_check`'s redundancy-skip).
- **Checkpoint recovery (#4)** — wire `_preserve_checkpoint` into the test agent (today only backend/frontend
  use it) so a cap-exhausted-but-green suite is preserved/recoverable, not discarded.
- **Promote via manifest + backup (#3)** — route the `tests/` promote through `promote_scratch_copy`'s
  manifest/backup path so test/harness changes are attributable and reversible.
- **Context strategy** — lean-context ON for the test agent: pull ledger/directives/contract/FE-map via tools
  **once** and let compaction fold them, rather than riding every ReAct turn.
- **Circuit breaker** — treat `run_e2e` infra soft-fails (missing browser, port bind) distinctly so they don't
  trip the 5-strike breaker mid-suite.
- **Verifier reconciliation** — the Supervisor deliberately skips AC-verification for `testing` stories; the
  **coverage ledger is the acceptance analog** for tests. State this explicitly so the two don't conflict.

### Loop shape decision
- **Option A (v1):** one ReAct loop covers all four layers, layers as internal milestones. Simplest; matches
  the repo's "thinnest orchestration" ethos.
- **Option B (only if A exhausts):** a thin per-layer **sequencer** runs harness/seed -> API -> UI/flows/
  integration as bounded ReAct sub-runs, each with its own cap, validator, and checkpoint. Still control flow,
  not a new reasoning core.
- **Ship A first; decompose to B only at a concrete wall** (observed cap exhaustion), per `plan.md`'s ethos.

---

## 9. Monitoring UI — dashboard page

New sidebar item **"Test Coverage"** (route `/test-coverage`), same React/Vite/Tailwind + dark OLED
design system. The thin existing `TestResults` page folds into it.

### Layout (`src/pages/TestCoverage.jsx`), project-scoped via the standard project selector

```
Test Coverage   [project v helpdesk]                      [> Run tests]
-------------------------------------------------------------------------
[ API 6/8  5v 1x ] [ UI Components 4/5 ] [ UI Flows 3/4 ] [ Integration 2/2 ]   <- summary cards

! Gaps (3)  --  DELETE /api/tickets/{id}  .  <TicketFilter>  .  "edit ticket"     <- gaps panel

[ API | Components | Flows | Integration | Guidance ]                             <- layer tabs
target                    test file            last run   status
POST /api/tickets         tests/api/test_...   2m ago     * passed
DELETE /api/tickets/{id}  --                   --         o not covered

Test Guidance (your directives)  [ + Add directive ]                              <- guidance tab
[high] New customer -> geocoded -> on map   linked: test_flows.py   * passed
[med]  Bulk import rejects bad rows          --                     o uncovered

Seed data  [ Edit ] [ Regenerate from contract ] [ Validate ] [ Seed live app ]   <- seed panel

Run history
* run_a1b2  manual  just now  14v 2x 1skip  32s  [expand v]   -> per-case results + output
* run_9f3c  build   12m ago   13v 0x        28s
```

- **Summary cards** — covered/total + pass/fail + coverage % per layer.
- **Gaps panel** — the agent's TODO surfaced to you.
- **Coverage tables** (tabbed by layer) — target -> test file -> last run -> status; not-covered rows distinct.
- **Test Guidance tab** — add/edit/reorder directives, categorize by flow kind, set priority; each shows a live
  status badge + linked test(s). An "unaddressed" filter surfaces your directives with no passing test yet.
- **Seed panel** — view/edit `seed_data.json` (validated on save), regenerate (backs up), validate, seed live app.
- **Run history** — build vs manual runs; expand for per-case pass/fail + output excerpt.
- **Run tests** button -> `POST /tests/run`, live status via existing polling.

Status-green = passed; accent-green = the Run button (per `DESIGN_SYSTEM.md` token separation).

### Backend routes (`dashboard/backend/routes/tests.py`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/tests/coverage?project_id=` | coverage-by-layer + gaps + totals (from mirror/manifest) |
| GET | `/tests/runs?project_id=&limit=` | run headers |
| GET | `/tests/runs/{id}` | header + per-case results + output excerpt |
| POST | `/tests/run` `{project_id, scope?}` | launch standalone run |
| GET | `/tests/run/status` | live run status |
| GET | `/tests/seed?project_id=` | read seed_data.json |
| PUT | `/tests/seed` `{project_id, data}` | save (contract-validated) |
| POST | `/tests/seed/regenerate` `{project_id}` | regenerate from contract (backs up first) |
| POST | `/tests/seed/apply-live` `{project_id}` | seed the running app for a demo |
| GET | `/tests/directives?project_id=` | list user directives + status |
| POST | `/tests/directives` `{project_id, directive}` | add a directive |
| PUT | `/tests/directives/{id}` | edit a directive |
| DELETE | `/tests/directives/{id}` | remove a directive |

New `api.js` methods for each. All reads come from the SQLite mirror (fast); `test_manifest.json` +
`test_directives.json` stay canonical.

---

## 10. Rollout phases (each independently shippable + testable)

1. **Ledger + recorder** — new tables/migrations, `test_ledger.py`, JUnit XML on `run_pytest`; wire into Supervisor Phase 3.
   *Verify: a build run produces a manifest + DB rows.*
2. **Harness scaffold** — canonical `tests/` scaffold (conftest/pytest.ini/requirements/factories) dropped in on first run (§7).
   *Verify: scaffold appears; agent adds test files on top without rewriting it.*
3. **`run_e2e` + layered test dirs + full context + testing DoD gate + loop tuning** — strengthen the agent (§2, §8).
   *Verify: agent authors api/ui/flows/integration tests; both validators go green; cap/budget/checkpoint tuned.*
4. **Dummy data** — `seed_data.json` / `factories.py`, `contract_sample` tool, temp-DB isolation.
   *Verify: populated screens render seeded data; API/flow tests self-seed on a temp DB.*
5. **Test Guidance** — `test_directives.json` + `test_directive` table + `read_test_directives` tool + directive gap analysis.
   *Verify: a user directive becomes a coverage target; uncovered -> gap; covered -> passed/failed status.*
6. **Standalone runner + endpoints** — `run_tests_background`, `POST /tests/run`.
   *Verify: re-run against an existing workspace with no rebuild.*
7. **Monitoring UI** — `TestCoverage.jsx` + routes + seed editor + Guidance tab.
   *Verify: coverage, gaps, directives, seed edit, history render per project; run button works.*
8. **Cumulative loop polish** — agent reads prior ledger + directives -> prioritizes user directives, then gaps, then regressions; gap-closure trend over runs.

---

## 11. Key design guardrails (folded in)

- Coverage/gaps computed from JUnit + static contract/FE analysis; agent annotations only enrich — a lazy or over-claiming agent can't inflate coverage.
- `ui_flow` vs `ui_component` vs `integration` inferred from the test directory — cheap, reliable, no agent-honesty dependency.
- `run_e2e` reuses `check_ui`'s boot/serve/proxy machinery rather than a new server stack.
- Full context and dummy-data generation respect the no-prompt-cache gateway (env-gated cost).
- Seed data **and** user directives are user-owned: validated on save, reversible on regenerate, lockable via `_managed`; the agent never deletes them.
- **No new reasoning core or orchestration engine** — reuse the ReAct loop + Supervisor; only tune the existing loop knobs (§8).
- Harness is a **stable canonical scaffold** the agent extends, not rebuilds — keeps runs deterministic and comparable across the ledger.
