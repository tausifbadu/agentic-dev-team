"""Test ledger — turn JUnit results into per-POC coverage + run history.

Called after the Test Agent runs (build Phase 3 today; a standalone runner later).
It writes the canonical workspace files —

    <workspace>/tests/test_manifest.json     # current coverage + gaps + totals
    <workspace>/tests/runs/run_<run_id>.json # full per-run detail

— and mirrors the same data into SQLite (test_run / test_case_result / test_coverage)
for the dashboard. Coverage and gaps are computed from machine-readable JUnit output
plus a static scan of the API contract and the produced test files — never from the
agent's own claims about what it tested.

Phase 1 scope: JUnit parsing, run recording, API-route coverage + gaps (the reliable
core), and per-test-case rows for the ui/flow/integration layers. Richer component /
flow / directive gap analysis lands in later phases.
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import state_store

# test dir fragment -> coverage layer
_LAYER_BY_DIR = (
    ("tests/api", "api"),
    ("tests/integration", "integration"),
    ("tests/flows", "ui_flow"),
    ("tests/ui", "ui_component"),
)
_LAYERS = ("api", "ui_component", "ui_flow", "integration")
_ACCEPT_ANNOT_RE = re.compile(r"#\s*covers:\s*([^\n]+)", re.IGNORECASE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def project_id_from_workspace(workspace_dir: Path) -> str:
    """Default workspace -> 'default'; workspace/projects/<slug> -> '<slug>'."""
    parts = Path(workspace_dir).parts
    if "projects" in parts:
        i = parts.index("projects")
        if i + 1 < len(parts):
            return parts[i + 1]
    return "default"


# --------------------------------------------------------------------------- #
# JUnit parsing
# --------------------------------------------------------------------------- #

def _infer_layer(test_file: str) -> str:
    tf = (test_file or "").replace("\\", "/").lower()
    for frag, layer in _LAYER_BY_DIR:
        if frag in tf:
            return layer
    if "integration" in tf:
        return "integration"
    if "flow" in tf:
        return "ui_flow"
    if "ui" in tf:
        return "ui_component"
    return "api"


def _file_from_classname(classname: str) -> str:
    """Fallback when JUnit omits the `file` attr: 'tests.api.test_x' -> 'tests/api/test_x.py'."""
    if not classname:
        return ""
    parts = classname.split(".")
    if parts and parts[-1][:1].isupper():  # drop a trailing Test* class name
        parts = parts[:-1]
    return "/".join(parts) + ".py" if parts else ""


def _parse_junit(paths: list[str]) -> list[dict]:
    cases: list[dict] = []
    for p in paths or []:
        fp = Path(p)
        if not fp.exists():
            continue
        try:
            root = ET.parse(fp).getroot()
        except (ET.ParseError, OSError):
            continue
        for tc in root.iter("testcase"):
            name = tc.get("name", "")
            classname = tc.get("classname", "")
            test_file = tc.get("file") or _file_from_classname(classname)
            status, message = "passed", ""
            for tag in ("failure", "error", "skipped"):
                el = tc.find(tag)
                if el is not None:
                    status = "skipped" if tag == "skipped" else "failed"
                    message = (el.get("message") or "") or (el.text or "")
                    break
            try:
                duration_ms = int(float(tc.get("time", "0")) * 1000)
            except (TypeError, ValueError):
                duration_ms = 0
            cases.append({
                "layer": _infer_layer(test_file),
                "test_file": test_file,
                "test_name": name,
                "status": status,
                "duration_ms": duration_ms,
                "message": (message or "").strip()[:2000],
            })
    return cases


# --------------------------------------------------------------------------- #
# Contract + test-file static analysis
# --------------------------------------------------------------------------- #

def _load_contract(workspace_dir: Path) -> dict:
    cf = workspace_dir / "contracts" / "api_contract.json"
    if cf.exists():
        try:
            return json.loads(cf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def _hash_contract(workspace_dir: Path) -> str:
    cf = workspace_dir / "contracts" / "api_contract.json"
    try:
        return hashlib.sha256(cf.read_bytes()).hexdigest() if cf.exists() else ""
    except OSError:
        return ""


def _routes(contract: dict) -> list[str]:
    out = []
    for r in (contract.get("routes") or []):
        method = (r.get("method") or "").upper()
        path = r.get("path") or ""
        if method and path:
            out.append(f"{method} {path}")
    return out


def _route_referenced(method: str, route_path: str, text: str) -> bool:
    """A route is 'referenced' if its literal path prefix (up to the first path
    param) appears in the test source AND the HTTP verb is actually exercised.
    Method-awareness is what distinguishes GET/POST/DELETE on the same path
    (e.g. `/api/tickets` vs `DELETE /api/tickets/{id}`)."""
    prefix = route_path.split("{", 1)[0].rstrip("/") or route_path
    if not prefix or prefix not in text:
        return False
    m = method.lower()
    return (
        f".{m}(" in text                       # client.get( / client.delete( ...
        or f'"{method}"' in text or f"'{method}'" in text
        or f"method={method!r}".lower() in text.lower()
    )


def _accept_refs(text: str) -> list[str]:
    refs: list[str] = []
    for m in _ACCEPT_ANNOT_RE.finditer(text or ""):
        refs.extend(x.strip() for x in re.split(r"[,\s]+", m.group(1)) if x.strip())
    return refs


def _read_test_files(tests_dir: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    if not tests_dir.exists():
        return files
    for p in tests_dir.rglob("*.py"):
        if p.name == "conftest.py":
            continue
        try:
            rel = "tests/" + str(p.relative_to(tests_dir)).replace("\\", "/")
            files[rel] = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return files


def _agg_status(cases: list[dict]) -> str:
    if any(c["status"] == "failed" for c in cases):
        return "failed"
    if cases and all(c["status"] == "skipped" for c in cases):
        return "skipped"
    if any(c["status"] == "passed" for c in cases):
        return "passed"
    return "covered"


# --------------------------------------------------------------------------- #
# Coverage + gaps
# --------------------------------------------------------------------------- #

def _build_coverage(cases: list[dict], contract: dict, tests_dir: Path,
                    run_id: str, now: str) -> tuple[dict, list[dict]]:
    files = _read_test_files(tests_dir)
    by_file: dict[str, list[dict]] = {}
    for c in cases:
        by_file.setdefault(c["test_file"], []).append(c)

    coverage: dict[str, list[dict]] = {layer: [] for layer in _LAYERS}
    gaps: list[dict] = []

    # --- API layer: coverage/gaps driven by the contract routes ---
    api_files = {f: t for f, t in files.items() if "tests/api" in f}
    for route in _routes(contract):
        method, _, path = route.partition(" ")
        matched = next(
            (f for f, t in api_files.items() if _route_referenced(method, path, t)), None)
        if matched:
            cs = by_file.get(matched, [])
            coverage["api"].append({
                "target": route,
                "test_file": matched,
                "test_names": [c["test_name"] for c in cs],
                "acceptance_refs": _accept_refs(api_files.get(matched, "")),
                "last_status": _agg_status(cs),
                "last_run_id": run_id,
                "last_run_at": now,
            })
        else:
            gaps.append({"kind": "api", "target": route,
                         "reason": "no test references this endpoint"})

    # --- ui_component / ui_flow / integration: one row per test case (Phase 1) ---
    for c in cases:
        if c["layer"] in ("ui_component", "ui_flow", "integration"):
            coverage[c["layer"]].append({
                "target": c["test_name"],
                "test_file": c["test_file"],
                "test_names": [c["test_name"]],
                "acceptance_refs": [],
                "last_status": c["status"],
                "last_run_id": run_id,
                "last_run_at": now,
            })
    return coverage, gaps


def _totals(coverage: dict, gaps: list[dict]) -> dict:
    out = {}
    for layer, rows in coverage.items():
        gap_n = sum(1 for g in gaps if _gap_layer(g["kind"]) == layer)
        out[layer] = {
            "covered": len(rows),
            "total": len(rows) + gap_n,
            "passed": sum(1 for r in rows if r["last_status"] == "passed"),
            "failed": sum(1 for r in rows if r["last_status"] == "failed"),
        }
    return out


def _gap_layer(kind: str) -> str:
    return "api" if kind == "api" else ("directive" if kind == "directive" else kind)


def _directive_coverage(cases: list[dict], files: dict[str, str],
                        directives: list[dict], run_id: str, now: str):
    """Attribute test cases to user directives via `# directive: <id>` annotations.

    Returns (updated_directives, gaps, db_rows). Each directive's status becomes
    passed/failed/covered from its linked cases (or stays uncovered); uncovered
    directives are emitted as gaps so they surface as the agent's TODO."""
    from agents.test_directives import parse_directive_refs

    by_file: dict[str, list[dict]] = {}
    for c in cases:
        by_file.setdefault(c["test_file"], []).append(c)
    # directive id -> cases (from any file annotated with that id)
    linked: dict[str, list[dict]] = {}
    for fpath, text in files.items():
        refs = parse_directive_refs(text)
        if not refs:
            continue
        for did in refs:
            linked.setdefault(did, []).extend(by_file.get(fpath, []))

    updated, gaps, db_rows = [], [], []
    for d in directives:
        did = d["id"]
        cs = linked.get(did, [])
        status = _agg_status(cs) if cs else "uncovered"
        test_ids = [f"{c['test_file']}::{c['test_name']}" for c in cs]
        nd = {**d, "status": status, "linked_test_ids": test_ids, "updated_at": now}
        updated.append(nd)
        db_rows.append({
            "layer": "directive", "target": f"[{d.get('priority','medium')}] {d.get('title') or did}",
            "test_file": (cs[0]["test_file"] if cs else ""),
            "test_names": [c["test_name"] for c in cs], "acceptance_refs": [did],
            "last_status": status, "last_run_id": run_id, "last_run_at": now,
            "is_gap": status == "uncovered",
            "reason": ("user directive not yet covered by any test" if status == "uncovered" else ""),
        })
        if status == "uncovered":
            gaps.append({"kind": "directive", "target": d.get("title") or did,
                         "reason": f"user directive '{did}' has no linked test",
                         "priority": d.get("priority", "medium")})
    return updated, gaps, db_rows


def _overall_status(cases: list[dict], fallback: str = "") -> str:
    if any(c["status"] == "failed" for c in cases):
        return "failed"
    if any(c["status"] == "passed" for c in cases):
        return "passed"
    return fallback or "no_tests"


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #

def _write_workspace_files(tests_dir: Path, run_id: str, manifest: dict,
                           cases: list[dict], trigger: str, status: str,
                           summary: str, now: str) -> None:
    try:
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / "test_manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")
        runs_dir = tests_dir / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / f"run_{run_id}.json").write_text(
            json.dumps({
                "run_id": run_id, "trigger": trigger, "status": status,
                "summary": summary, "at": now, "cases": cases,
            }, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _coverage_rows_for_db(coverage: dict, gaps: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for layer, items in coverage.items():
        for r in items:
            rows.append({**r, "layer": layer, "is_gap": False, "reason": ""})
    for g in gaps:
        rows.append({
            "layer": _gap_layer(g["kind"]), "target": g["target"],
            "test_file": "", "test_names": [], "acceptance_refs": [],
            "last_status": "uncovered", "last_run_id": "", "last_run_at": "",
            "is_gap": True, "reason": g.get("reason", ""),
        })
    return rows


def _mirror_db(*, project_id: str, storypack_id: str, run_id: str, trigger: str,
               status: str, totals: dict, summary: str, cases: list[dict],
               coverage: dict, gaps: list[dict], started_at: Optional[str],
               finished_at: Optional[str], extra_rows: Optional[list[dict]] = None) -> None:
    try:
        rid = state_store.save_test_run(
            project_id=project_id, storypack_id=storypack_id, run_id=run_id,
            trigger=trigger, status=status, totals=totals, summary=summary,
            started_at=started_at, finished_at=finished_at,
        )
        state_store.save_test_case_results(
            rid, project_id,
            [{**c, "target": c.get("test_name", "")} for c in cases],
        )
        rows = _coverage_rows_for_db(coverage, gaps) + list(extra_rows or [])
        state_store.upsert_test_coverage(project_id, rows)
    except Exception:  # noqa: BLE001 — recording must never break a run
        pass


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def record_test_run(
    *,
    workspace_dir: Any,
    run_id: str,
    storypack_id: str = "",
    trigger: str = "build",
    junit_paths: Optional[list[str]] = None,
    stories: Optional[list] = None,     # reserved for later gap analysis
    contract: Optional[dict] = None,
    status: str = "",
    summary: str = "",
    tests_source: Any = None,           # scan this dir for test files (defaults to <ws>/tests)
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
) -> dict:
    """Record one test run: parse JUnit, compute coverage/gaps, write the workspace
    ledger files, and mirror to SQLite. Returns the manifest dict."""
    workspace_dir = Path(workspace_dir)
    project_id = project_id_from_workspace(workspace_dir)
    tests_dir = workspace_dir / "tests"
    scan_dir = Path(tests_source) if tests_source else tests_dir
    contract = contract if contract is not None else _load_contract(workspace_dir)
    now = _now()

    cases = _parse_junit(junit_paths or [])
    coverage, gaps = _build_coverage(cases, contract, scan_dir, run_id, now)
    totals = _totals(coverage, gaps)
    overall = status or _overall_status(cases)

    # --- User directives (Test Guidance): attribute tests, update status, surface gaps.
    # Directives are read from the canonical live tests dir (user-owned); the tests that
    # ran are scanned from scan_dir. Updated status is written back to the file + DB.
    from agents.test_directives import load_directives, save_directives, sync_to_db
    directives = load_directives(tests_dir)
    directive_rows: list[dict] = []
    directive_gaps: list[dict] = []
    if directives:
        files = _read_test_files(scan_dir)
        directives, directive_gaps, directive_rows = _directive_coverage(
            cases, files, directives, run_id, now)
        save_directives(tests_dir, directives)
        sync_to_db(project_id, directives)
        totals["directives"] = {
            "covered": sum(1 for d in directives if d["status"] != "uncovered"),
            "total": len(directives),
            "passed": sum(1 for d in directives if d["status"] == "passed"),
            "failed": sum(1 for d in directives if d["status"] == "failed"),
        }

    all_gaps = gaps + directive_gaps
    # Record run-level counts so a gap-closure trend can be computed across runs.
    totals["_meta"] = {"gaps": len(all_gaps), "cases": len(cases)}

    manifest = {
        "project_id": project_id,
        "updated_at": now,
        "contract_hash": _hash_contract(workspace_dir),
        "coverage": coverage,
        "directives": directives,
        "gaps": all_gaps,
        "totals": totals,
        "last_run": {
            "run_id": run_id, "trigger": trigger, "status": overall,
            "summary": (summary or "")[:500], "at": now, "cases": len(cases),
        },
    }

    _write_workspace_files(tests_dir, run_id, manifest, cases, trigger, overall,
                           (summary or "")[:500], now)
    # Pass non-directive gaps to the generic row builder; directive rows are supplied
    # separately (already shaped) so they aren't double-counted.
    _mirror_db(
        project_id=project_id, storypack_id=storypack_id, run_id=run_id,
        trigger=trigger, status=overall, totals=totals, summary=summary,
        cases=cases, coverage=coverage, gaps=gaps,
        started_at=started_at, finished_at=finished_at, extra_rows=directive_rows,
    )
    return manifest
