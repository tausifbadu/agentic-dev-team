"""Test results + coverage + guidance API routes."""

import sys
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import state_store
from agents.reasoning import load_json
from workspace_paths import normalize_project_id, resolve_workspace_dir
from dashboard.backend.execution import test_run_state, run_tests_background

router = APIRouter(tags=["tests"])


# ----------------------------- legacy results -----------------------------

@router.get("/tests/results")
def get_test_results(storypack_id: str | None = None, limit: int = 50):
    return state_store.get_test_results(storypack_id=storypack_id, limit=limit)


# ----------------------------- run control ---------------------------------

class RunTestsBody(BaseModel):
    project_id: str = "default"


@router.post("/tests/run")
def run_tests(body: RunTestsBody):
    if test_run_state.get("running"):
        raise HTTPException(409, "A test run is already in progress. Wait for it to finish.")
    pid = normalize_project_id(body.project_id)
    thread = threading.Thread(target=run_tests_background, args=(pid,), daemon=True)
    thread.start()
    return {"status": "started", "project_id": pid}


@router.get("/tests/run/status")
def get_test_run_status():
    return test_run_state.copy()


# ----------------------------- coverage + runs -----------------------------

@router.get("/tests/coverage")
def get_coverage(project_id: str = "default"):
    """Coverage-by-layer + gaps + directives + totals for a project."""
    pid = normalize_project_id(project_id)
    rows = state_store.get_test_coverage(pid)
    layers: dict[str, list] = {}
    gaps: list[dict] = []
    for r in rows:
        if r.get("is_gap"):
            gaps.append(r)
        layers.setdefault(r["layer"], []).append(r)
    manifest = load_json(resolve_workspace_dir(pid) / "tests" / "test_manifest.json", {})
    # Gap-closure trend: gap count per run (oldest→newest) from recorded run totals.
    import json as _json
    trend = []
    for r in reversed(state_store.list_test_runs(pid, limit=15)):
        try:
            meta = (_json.loads(r.get("totals_json") or "{}")).get("_meta", {})
        except (ValueError, TypeError):
            meta = {}
        trend.append({
            "run_id": r.get("run_id", ""), "status": r.get("status", ""),
            "gaps": meta.get("gaps", 0), "cases": meta.get("cases", 0),
            "at": r.get("finished_at") or r.get("started_at", ""),
        })
    return {
        "project_id": pid,
        "layers": layers,
        "gaps": gaps,
        "totals": manifest.get("totals", {}),
        "trend": trend,
        "updated_at": manifest.get("updated_at", ""),
        "last_run": manifest.get("last_run", {}),
    }


@router.get("/tests/runs")
def list_runs(project_id: str = "default", limit: int = 50):
    return state_store.list_test_runs(normalize_project_id(project_id), limit=limit)


@router.get("/tests/runs/{run_row_id}")
def get_run(run_row_id: int):
    run = state_store.get_test_run(run_row_id)
    if not run:
        raise HTTPException(404, "Test run not found")
    return run


# ----------------------------- directives ----------------------------------

class DirectiveBody(BaseModel):
    project_id: str = "default"
    id: str | None = None
    kind: str = "business_flow"
    title: str = ""
    steps: list[str] = []
    expected_outcome: str = ""
    priority: str = "medium"


def _write_directives(pid: str, directives: list[dict]) -> None:
    """Persist directives to the canonical file + DB mirror."""
    from agents.test_directives import save_directives, sync_to_db
    save_directives(resolve_workspace_dir(pid) / "tests", directives)
    sync_to_db(pid, directives)


@router.get("/tests/directives")
def list_directives(project_id: str = "default"):
    return state_store.list_test_directives(normalize_project_id(project_id))


@router.post("/tests/directives")
def add_directive(body: DirectiveBody):
    from agents.test_directives import load_directives, normalize_directive
    pid = normalize_project_id(body.project_id)
    tests_dir = resolve_workspace_dir(pid) / "tests"
    directives = load_directives(tests_dir)
    new = normalize_directive(body.model_dump(exclude={"project_id"}), index=len(directives))
    directives.append(new)
    _write_directives(pid, directives)
    return new


@router.put("/tests/directives/{directive_id}")
def update_directive(directive_id: str, body: DirectiveBody):
    from agents.test_directives import load_directives, normalize_directive
    pid = normalize_project_id(body.project_id)
    tests_dir = resolve_workspace_dir(pid) / "tests"
    directives = load_directives(tests_dir)
    found = False
    for i, d in enumerate(directives):
        if d["id"] == directive_id:
            merged = {**d, **body.model_dump(exclude={"project_id"}), "id": directive_id}
            directives[i] = normalize_directive(merged, index=i)
            found = True
            break
    if not found:
        raise HTTPException(404, "Directive not found")
    _write_directives(pid, directives)
    return directives[i]


@router.delete("/tests/directives/{directive_id}")
def delete_directive(directive_id: str, project_id: str = "default"):
    from agents.test_directives import load_directives
    pid = normalize_project_id(project_id)
    tests_dir = resolve_workspace_dir(pid) / "tests"
    directives = [d for d in load_directives(tests_dir) if d["id"] != directive_id]
    _write_directives(pid, directives)
    return {"status": "deleted", "id": directive_id}


# ----------------------------- seed data -----------------------------------

class SeedBody(BaseModel):
    project_id: str = "default"
    data: dict


@router.get("/tests/seed")
def get_seed(project_id: str = "default"):
    pid = normalize_project_id(project_id)
    return load_json(resolve_workspace_dir(pid) / "tests" / "seed_data.json", {})


@router.put("/tests/seed")
def save_seed(body: SeedBody):
    """Save seed data, validated against the API contract (enum vocabulary)."""
    from agents.test_seed import validate_seed_payload
    import json
    pid = normalize_project_id(body.project_id)
    ws = resolve_workspace_dir(pid)
    contract = load_json(ws / "contracts" / "api_contract.json", {})
    issues = validate_seed_payload(body.data, contract) if contract else []
    if issues:
        raise HTTPException(400, {"message": "Seed data has contract violations", "issues": issues})
    seed_path = ws / "tests" / "seed_data.json"
    try:
        seed_path.parent.mkdir(parents=True, exist_ok=True)
        seed_path.write_text(json.dumps(body.data, indent=2), encoding="utf-8")
    except OSError as exc:
        raise HTTPException(500, f"Failed to write seed data: {exc}")
    return {"status": "saved", "project_id": pid}


@router.post("/tests/seed/regenerate")
def regenerate_seed(project_id: str = "default"):
    """Regenerate seed data from the contract (backs up the previous file first)."""
    from agents.test_seed import ensure_seed_data
    pid = normalize_project_id(project_id)
    ws = resolve_workspace_dir(pid)
    contract = load_json(ws / "contracts" / "api_contract.json", {})
    if not contract:
        raise HTTPException(400, "No API contract published for this project yet.")
    res = ensure_seed_data(ws / "tests", contract, regenerate=True)
    if res.get("status") == "locked":
        raise HTTPException(409, "Seed data is marked _managed=manual and was not overwritten.")
    return res
