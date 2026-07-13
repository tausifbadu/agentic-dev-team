"""Canonical test-harness scaffold for a POC's `tests/` directory.

Mirrors the frontend's `_ensure_project_scaffold`: a stable harness (fixtures +
pytest config + test deps) is dropped into `tests/` **once** and thereafter reused —
the Test Agent only ADDS test files on top, it never rebuilds the harness. A stable
harness is what makes runs deterministic and comparable across the ledger, and it
saves iterations (the agent doesn't re-derive fixtures every run against a gateway
with no prompt caching).

`ensure_test_scaffold` is create-if-missing: it writes each scaffold file only when
absent, so any later hand-edit (or agent extension) is preserved on subsequent runs.
"""

from __future__ import annotations

from pathlib import Path

_CONFTEST = '''"""Canonical test harness for this POC (auto-scaffolded — extend, do NOT rewrite).

Shared fixtures for every test layer:
  - app / client : in-process FastAPI app + TestClient (API + in-process integration)
  - base_url     : URL of a live server (set by run_e2e via BASE_URL) for UI / E2E
  - api_client   : httpx client bound to base_url (live flow / integration setup)
  - seed_data    : parsed tests/seed_data.json (empty until the dummy-data phase)

Playwright's `page` / `browser` / `context` fixtures come from pytest-playwright.
Add test files under tests/api, tests/ui, tests/flows, tests/integration. Do NOT edit
this file or pytest.ini — the runtime treats them as the stable harness.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent

# --- Database isolation --------------------------------------------------------
# Point the app at a throwaway SQLite DB so tests never touch the dev database,
# for backends that read their DB path from an env var. Set at import time (before
# the app is imported by the `app` fixture). setdefault() respects an already-set
# value, and backends that hardcode their path are simply unaffected — those tests
# should self-seed on an empty DB via the API rather than rely on this.
_TEST_DB = os.path.join(tempfile.mkdtemp(prefix="poc_testdb_"), "test.db")
for _v in ("DATABASE_URL", "DB_URL", "SQLALCHEMY_DATABASE_URL", "DATABASE_URI"):
    os.environ.setdefault(_v, f"sqlite:///{_TEST_DB}")
for _v in ("DB_PATH", "DATABASE_PATH", "SQLITE_PATH", "DB_FILE"):
    os.environ.setdefault(_v, _TEST_DB)


@pytest.fixture(scope="session")
def app():
    """The FastAPI app under test, imported exactly as the backend runs it.
    (run_pytest puts the backend dir on PYTHONPATH, so `from main import app` works.)"""
    from main import app as _app
    return _app


@pytest.fixture()
def client(app):
    """In-process API client. Context-managed so FastAPI lifespan startup runs and
    app.state is initialized (routes reading app.state would otherwise raise)."""
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def base_url() -> str:
    """Base URL of a live server for UI/E2E + live-integration tests. run_e2e sets
    BASE_URL when it boots the backend and serves the built frontend."""
    return os.environ.get("BASE_URL", "http://127.0.0.1:8000")


@pytest.fixture()
def api_client(base_url):
    """httpx client bound to a live server (flow / integration setup)."""
    import httpx
    with httpx.Client(base_url=base_url, timeout=10.0) as c:
        yield c


@pytest.fixture(scope="session")
def seed_data():
    """Parsed tests/seed_data.json (or {} if absent). Populated in the dummy-data phase."""
    f = _TESTS_DIR / "seed_data.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return {}
'''

_PYTEST_INI = """[pytest]
# Layer markers — the test ledger also infers layer from the directory, but marking
# tests keeps intent explicit and lets run_e2e select layers cleanly.
markers =
    api: API endpoint tests (status codes, validation, contract)
    ui: UI component-presence tests (Playwright)
    flow: UI user-flow / end-to-end tests (Playwright)
    integration: backend integration-flow tests (live app)
log_cli_level = WARNING
"""

_TESTS_REQUIREMENTS = """pytest
httpx
playwright
pytest-playwright
"""

_FACTORIES = '''"""Data builders for tests (auto-scaffolded — extend as needed).

Prefer building test data through the API (exercises the write path and keeps data
contract-valid) over inserting into the DB directly.

`seed_data.json` (generated from the API contract, enum-valid) provides realistic
rows for populated-list / filter / pagination screens. Use `load_seed()` to read it
and `seed_via_api()` to POST those rows into a running app (UI/flow/integration).
For one-off valid payloads, the agent can also call the `contract_sample` tool.
"""
from __future__ import annotations

import json
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent


def load_seed() -> dict:
    """Return the parsed tests/seed_data.json data map ({} if absent)."""
    f = _TESTS_DIR / "seed_data.json"
    if f.exists():
        try:
            return (json.loads(f.read_text(encoding="utf-8")) or {}).get("data", {})
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def seed_via_api(client, endpoint: str, model: str, limit: int = 0) -> list:
    """POST seed rows for `model` to `endpoint` on a running app; return created rows.

        created = seed_via_api(api_client, "/api/tickets", "TicketCreateRequest")

    `client` is any object with `.post(url, json=...)` (httpx client or TestClient)."""
    rows = load_seed().get(model, [])
    if limit:
        rows = rows[:limit]
    created = []
    for row in rows:
        r = client.post(endpoint, json=row)
        if getattr(r, "status_code", 0) in (200, 201):
            created.append(r.json())
    return created


def make(client, endpoint: str, **fields):
    """POST one row to `endpoint`, asserting a 2xx create. Returns the created row."""
    r = client.post(endpoint, json=fields)
    assert getattr(r, "status_code", 0) in (200, 201), getattr(r, "text", r)
    return r.json()
'''

# filename -> content (harness files only)
_SCAFFOLD_FILES = {
    "conftest.py": _CONFTEST,
    "pytest.ini": _PYTEST_INI,
    "requirements.txt": _TESTS_REQUIREMENTS,
    "factories.py": _FACTORIES,
}
_SUBDIRS = ("api", "ui", "flows", "integration")


def ensure_test_scaffold(tests_dir) -> list[str]:
    """Create the canonical harness in `tests_dir` if missing. Returns the list of
    files newly created (empty if the scaffold already existed). Never overwrites."""
    tests_dir = Path(tests_dir)
    created: list[str] = []
    try:
        tests_dir.mkdir(parents=True, exist_ok=True)
        for sub in _SUBDIRS:
            (tests_dir / sub).mkdir(parents=True, exist_ok=True)
        for name, content in _SCAFFOLD_FILES.items():
            f = tests_dir / name
            if not f.exists():
                f.write_text(content, encoding="utf-8")
                created.append(name)
    except OSError:
        pass
    return created
