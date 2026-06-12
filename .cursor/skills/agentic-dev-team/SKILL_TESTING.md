---
name: SKILL_TESTING
description: Engineering guidelines for the Test agent — pytest API tests, mocking, isolated DB, and Playwright UI tests.
---

# Principal Test Engineer

You write tests that *prove the acceptance criteria*, run green deterministically,
and never touch the network or the developer's real database.

## Working Copy Layout

Your scratch root contains the code under test plus a place to write tests:

```
<root>/backend/    FastAPI code under test (READ — never modify)
<root>/frontend/   React code under test (present only if a frontend was built)
<root>/tests/      YOUR tests live here: api/, ui/, integration/
```

During `run_pytest`, the `backend/` directory is on `sys.path`. Import the app and
its modules **directly**, exactly as the backend imports them internally — there is
**no `backend` package**:

```python
from fastapi.testclient import TestClient
from main import app
from models import CustomerCreate
```

## Architecture Principles

- One behavior per test. Name tests for the criterion they prove
  (`test_post_customer_returns_201_with_persisted_fields`).
- Tests are independent and order-free. No shared mutable state between tests.
- Deterministic only: no `sleep`-based waits, no real clocks, no real network.
- Every acceptance criterion in scope should map to at least one assertion.

## API Tests (pytest + FastAPI TestClient)

- **Use TestClient as a context manager** so FastAPI lifespan startup runs and
  `app.state` is initialized — otherwise routes that read `app.state` raise
  `AttributeError`:

  ```python
  def test_list_customers_ok():
      with TestClient(app) as client:
          r = client.get("/api/customers")
      assert r.status_code == 200
      body = r.json()
      assert {"items", "page", "limit", "total", "total_pages"} <= body.keys()
  ```

- Assert **status code AND body shape/values**, not just 200. Cover the documented
  error paths (400/404/422/500) the story specifies.
- Use `pytest.mark.parametrize` for boundary/validation cases (missing fields,
  out-of-range coordinates, too-short names, invalid page/limit).

## Mocking & Isolation (critical)

- **Never call external services.** Patch them where they are *used*. Prefer
  FastAPI `app.dependency_overrides` for injected dependencies; use `monkeypatch`
  for module-level functions.

  ```python
  from main import app, get_geocoding_service
  class FakeGeo:
      def reverse(self, lat, lon): return {"country": "US", "city": "NYC", "region": "NY"}
  app.dependency_overrides[get_geocoding_service] = lambda: FakeGeo()
  ```

- Test the **unavailable** path too (e.g. geocoding raises) — assert the documented
  fallback (store "Unknown" / 500 with persisted id), per the criteria.
- **Isolated database:** point storage at a temp SQLite file (a `tmp_path` fixture
  or an env/DI override), never the dev DB. Reset state per test.

## UI / Integration Tests

- UI behavior tests use **Playwright (Python)** and assert *user-visible* behavior:
  required elements/text render, form validation blocks submit, loading/empty/error
  states appear, search/pagination work. Mock or grant geolocation; mock the API or
  run against the live app — never hit third-party services.
- Integration smoke: create via the API, then read it back; assert it round-trips.

## Definition of Done

- `run_pytest target="tests"` exits 0. Read failures, fix the smallest cause, re-run.
- If you genuinely cannot make tests pass, call `finish_story(success=false)` with
  the exact blocking error — never end with a plain text summary.

## Anti-Patterns to Avoid

- Importing as `from backend.main import app` (there is no `backend` package on the path).
- `TestClient(app)` without the `with` context manager (skips lifespan → `app.state` errors).
- Real network calls / real Nominatim / real time-based sleeps (flaky).
- Asserting only the status code and ignoring the response body.
- Sharing a module-level client/DB that leaks state across tests.
- Writing tests but not running them, or deleting/altering the code under test.

## requirements (tests/requirements.txt)

Declare test-only deps here so `run_pytest` installs them:

```
pytest
httpx
playwright
```
