"""Background agent execution for the dashboard API."""

import difflib
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import state_store
from workspace_paths import resolve_workspace_dir
from schemas import Story
from agents.backend_agent import implement_backend
from agents.frontend_agent import implement_frontend
from agents.test_agent import implement_tests
from agents.pm_agent import (
    analyze_failure,
    create_enhancement_story,
    create_enhancement_stories_both,
    replan_failure,
    rescope_story,
    triage_test_failure,
)
from agents.reasoning import (
    backup_workspace,
    discard_staging,
    extract_api_contract,
    promote_workspace,
    save_json,
    stage_workspace,
)

# Toggle the new agentic supervisor runtime. Set USE_LEGACY_PIPELINE=1 to fall
# back to the original script-driven orchestration (kept for safety).
USE_AGENTIC_SUPERVISOR = os.getenv("USE_LEGACY_PIPELINE", "0").lower() not in ("1", "true", "yes")

MAX_PM_RETRIES = 3
MAX_TEST_FIX_CYCLES = 3
MAX_SMOKE_FIX_CYCLES = 3


def _env_truthy(name: str) -> bool:
    v = os.getenv(name, "").strip().lower()
    return v in ("1", "true", "yes")


def _workspace_for_pack(pack: dict) -> Path:
    """Resolve filesystem root for a storypack (or fix request pack) from ``project_id``."""
    return resolve_workspace_dir(pack.get("project_id"))


execution_state: dict = {
    "running": False,
    "current_agent": None,
    "current_story_id": None,
    "storypack_id": None,
    "completed_stories": [],
    "failed_stories": [],
    "skipped_stories": [],
    "phase": "idle",
    "conclusion": None,
}

# Active supervisor (set when the agentic runtime is mid-run) so endpoints can
# read its live budget snapshot without holding direct references.
_active_supervisor: object | None = None


def get_active_budget_snapshot() -> dict:
    """Return the live budget snapshot if a supervisor run is in flight."""
    sup = _active_supervisor
    if sup is None or not getattr(sup, "budget", None):
        return {}
    try:
        return sup.budget.snapshot()
    except Exception:
        return {}


def derive_status_from_db() -> dict:
    """Reconstruct run status from the durable DB (the single source of truth).

    `execution_state` is an in-memory cache local to *this* process, so it is wrong
    whenever the run executes elsewhere (CLI) or the server was reloaded mid-run.
    This rebuilds status from agent_comms/storypacks so the dashboard is correct
    regardless of how/where the run was triggered, and after a reload.
    """
    status = {
        "running": False, "current_agent": None, "current_story_id": None,
        "storypack_id": None, "run_id": None, "completed_stories": [],
        "failed_stories": [], "skipped_stories": [], "phase": "idle",
        "conclusion": None, "runtime": "agentic", "source": "db",
    }
    try:
        packs = state_store.list_storypacks()
    except Exception:
        return status
    if not packs:
        return status

    pack = packs[0]  # list_storypacks() is newest-first
    pack_id = pack.get("id")
    pstatus = pack.get("status")
    status["storypack_id"] = pack_id

    try:
        comms = state_store.get_agent_comms(storypack_id=pack_id, limit=2000)
    except Exception:
        comms = []
    comms = sorted(comms, key=lambda c: c.get("id", 0))

    final_story: dict[str, str] = {}
    last_assigned = None
    end_event = None
    run_id = None
    for c in comms:
        et = c.get("event_type")
        if c.get("run_id"):
            run_id = c["run_id"]
        if et == "story.assigned":
            last_assigned = c
        elif et in ("story.completed", "story.failed"):
            sid = c.get("story_id")
            if sid:
                final_story[sid] = et
        elif et in ("run.completed", "run.failed"):
            end_event = c
    status["run_id"] = run_id
    status["completed_stories"] = [s for s, e in final_story.items() if e == "story.completed"]
    status["failed_stories"] = [s for s, e in final_story.items() if e == "story.failed"]

    if end_event is not None:
        status["phase"] = "done"
        status["running"] = False
        try:
            payload = json.loads(end_event.get("payload_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {}
        status["conclusion"] = payload.get("conclusion") or (
            "completed" if end_event.get("event_type") == "run.completed" else "failed"
        )
    elif pstatus == "in_progress":
        status["running"] = True
        if last_assigned is not None:
            status["current_story_id"] = last_assigned.get("story_id")
            try:
                p = json.loads(last_assigned.get("payload_json") or "{}")
                status["current_agent"] = p.get("agent")
            except (json.JSONDecodeError, TypeError):
                pass
            status["phase"] = status["current_agent"] or "running"
    return status


def derive_budget_from_db(pack_id: str, run_id: str | None, tool_calls_used: int) -> dict:
    """Reconstruct a budget snapshot from the DB for runs not live in this process
    (so the Live console shows real tool-call/wall-clock usage, not 0/0)."""
    import datetime
    budget = {
        "max_tool_calls": int(os.getenv("AGENTIC_MAX_TOOL_CALLS", "600")),
        "max_wall_seconds": float(os.getenv("AGENTIC_MAX_WALL_SECONDS", "1800")),
        "tool_calls_used": int(tool_calls_used),
        "elapsed_seconds": 0.0,
        "remaining_seconds": 0.0,
    }
    try:
        comms = state_store.get_agent_comms(storypack_id=pack_id, run_id=run_id, limit=2000)
        ts = sorted(c["created_at"] for c in comms if c.get("created_at"))
        if len(ts) >= 2:
            a = datetime.datetime.fromisoformat(ts[0])
            z = datetime.datetime.fromisoformat(ts[-1])
            elapsed = (z - a).total_seconds()
            budget["elapsed_seconds"] = round(elapsed, 1)
            budget["remaining_seconds"] = round(max(0.0, budget["max_wall_seconds"] - elapsed), 1)
    except Exception:
        pass
    return budget


def current_run_status() -> dict:
    """Authoritative run status for the API.

    Prefer the in-memory `execution_state` only while a run is genuinely live in
    THIS process (most granular), or when it holds the finished result of the same
    latest run. Otherwise reconstruct from the DB.
    """
    es = execution_state
    if es.get("running"):
        return {**es, "source": "memory"}
    db = derive_status_from_db()
    if (
        es.get("storypack_id")
        and es.get("storypack_id") == db.get("storypack_id")
        and es.get("conclusion")
    ):
        return {**es, "source": "memory"}
    return db


def get_active_bus():
    """Return the bus of the in-flight Supervisor (if any), else None.

    The SSE endpoint uses this to subscribe to live agent traffic. When no
    supervisor is running the endpoint falls back to tailing agent_comms.
    """
    sup = _active_supervisor
    return getattr(sup, "bus", None) if sup is not None else None


def _log(story_id: str | None, agent: str, msg: str, level: str = "info",
         detail: str | None = None):
    try:
        state_store.add_agent_log(story_id, agent, msg, level=level, detail=detail)
    except Exception as exc:
        print(f"[_log] Failed to write log (non-fatal): {exc} | agent={agent} msg={msg[:200]}", flush=True)


def _make_progress_cb(story_id: str | None, agent: str):
    def on_progress(message: str, detail: str | None = None):
        _log(story_id, agent, message, detail=detail)
    return on_progress


# ── Structured Communication ──

_current_run_id: str = ""
_current_pack_id: str = ""


def _comm(from_agent: str, to_agent: str, event_type: str,
          story_id: str | None = None, cycle: int = 0,
          payload: dict | None = None, summary: str = ""):
    try:
        state_store.save_agent_comm(
            _current_pack_id or "none", _current_run_id or "none",
            from_agent, to_agent, event_type,
            story_id, cycle, json.dumps(payload or {}, default=str), summary,
        )
    except Exception as exc:
        print(f"[_comm] Failed to save agent comm (non-fatal): {exc}", flush=True)


# ── Dependency Sort ──

def expand_story_selection(
    all_stories: list[Story],
    selected_ids: list[str] | None,
) -> tuple[list[Story], list[str]]:
    """Resolve which stories to run.

    If ``selected_ids`` is None or empty, returns ``all_stories`` unchanged.

    Otherwise returns the selected stories plus every transitive prerequisite
    (``Story.dependencies`` edges within the pack). Order matches ``all_stories``.
    The second return value lists ids that were auto-included as dependencies.
    """
    if not selected_ids:
        return (all_stories, [])
    by_id = {s.id: s for s in all_stories}
    missing = [x for x in selected_ids if x not in by_id]
    if missing:
        raise ValueError(f"Unknown story id(s): {missing}")

    closure: set[str] = set(selected_ids)
    queue: list[str] = list(selected_ids)
    while queue:
        sid = queue.pop(0)
        if sid not in by_id:
            continue
        for dep in by_id[sid].dependencies:
            if dep in by_id and dep not in closure:
                closure.add(dep)
                queue.append(dep)

    selected_set = set(selected_ids)
    auto_added = sorted(closure - selected_set)
    ordered = [s for s in all_stories if s.id in closure]
    return (ordered, auto_added)


def _topo_sort_within_phase(stories: list[Story]) -> list[Story]:
    """Topological sort respecting story.dependencies within a phase."""
    id_set = {s.id for s in stories}
    by_id = {s.id: s for s in stories}
    in_degree: dict[str, int] = {s.id: 0 for s in stories}
    for s in stories:
        for dep in s.dependencies:
            if dep in id_set:
                in_degree[s.id] = in_degree.get(s.id, 0) + 1

    queue = [sid for sid, deg in in_degree.items() if deg == 0]
    ordered: list[Story] = []
    while queue:
        queue.sort()
        sid = queue.pop(0)
        ordered.append(by_id[sid])
        for s in stories:
            if sid in s.dependencies and s.id in id_set:
                in_degree[s.id] -= 1
                if in_degree[s.id] == 0:
                    queue.append(s.id)

    remaining = [s for s in stories if s not in ordered]
    ordered.extend(remaining)
    return ordered


# ── Contract Publish ──

def _publish_contract(workspace_root: Path) -> dict:
    backend_dir = workspace_root / "backend"
    contracts_dir = workspace_root / "contracts"
    contract = extract_api_contract(backend_dir)
    contracts_dir.mkdir(parents=True, exist_ok=True)
    save_json(contracts_dir / "api_contract.json", contract)
    route_count = len(contract.get("routes", []))
    model_count = len(contract.get("models", []))
    _log(None, "orchestrator",
         f"Published API contract: {route_count} routes, {model_count} models",
         detail=json.dumps(contract, indent=2))
    _comm("backend", "frontend", "contract_publish",
          payload=contract,
          summary=f"{route_count} routes, {model_count} models")
    return contract


# ── Heal Story ──

def _heal_story(
    story: Story,
    agent_type: str,
    implement_fn,
    requirement_text: str,
    all_stories: list[Story],
    initial_error: str,
    *,
    workspace_root: Path | None = None,
) -> tuple[bool, str]:
    """PM-guided retry loop for a single story (up to MAX_PM_RETRIES)."""
    cumulative_errors = initial_error
    progress = _make_progress_cb(story.id, agent_type)

    for attempt in range(1, MAX_PM_RETRIES + 1):
        _log(story.id, "orchestrator",
             f"[heal cycle {attempt}/{MAX_PM_RETRIES}] {agent_type.title()} failed on '{story.title}' — PM analyzing",
             detail=f"Error:\n{initial_error if attempt == 1 else cumulative_errors[-2000:]}")
        _comm("orchestrator", "pm", "heal_request", story.id, attempt,
              {"error": cumulative_errors[-2000:]},
              f"Heal cycle {attempt} for '{story.title}'")

        use_replan = attempt >= 2
        try:
            if use_replan:
                _log(story.id, "pm", f"[heal cycle {attempt}/{MAX_PM_RETRIES}] Re-planning '{story.title}'")
                fix_instructions = replan_failure(story, cumulative_errors, requirement_text)
                _log(story.id, "pm", "Revised plan ready", detail=fix_instructions)
                _comm("pm", "orchestrator", "replan", story.id, attempt,
                      {"instructions": fix_instructions[:1000]},
                      f"Replan for '{story.title}'")
            else:
                _log(story.id, "pm", f"[heal cycle {attempt}/{MAX_PM_RETRIES}] Analyzing failure for '{story.title}'")
                fix_instructions = analyze_failure(story, cumulative_errors, requirement_text)
                _log(story.id, "pm", "Fix instructions ready", detail=fix_instructions)
                _comm("pm", "orchestrator", "fix_instructions", story.id, attempt,
                      {"instructions": fix_instructions[:1000]},
                      f"Fix guidance for '{story.title}'")

            _comm("orchestrator", agent_type, "heal_handoff", story.id, attempt,
                  {"replan": use_replan},
                  f"PM → {agent_type.title()}: retry '{story.title}'")
        except Exception as exc:
            fix_instructions = f"PM analysis failed: {exc}. Original error:\n{cumulative_errors}"
            _log(story.id, "pm", f"PM analysis error: {exc}", level="error")

        _log(story.id, agent_type, f"[heal cycle {attempt}/{MAX_PM_RETRIES}] Retrying: {story.title}")
        fn_kwargs: dict = {}
        if workspace_root is not None:
            fn_kwargs["workspace_root"] = workspace_root
        ok, msg = implement_fn(
            story, requirement_text, all_stories,
            on_progress=progress,
            fix_context=fix_instructions,
            replan=use_replan,
            **fn_kwargs,
        )

        if ok:
            _log(story.id, "orchestrator", f"[heal cycle {attempt}] Auto-heal SUCCEEDED for '{story.title}'")
            _comm(agent_type, "orchestrator", "heal_outcome", story.id, attempt,
                  {"success": True}, f"Heal succeeded on cycle {attempt}")
            state_store.save_failure_pattern(
                _current_pack_id, story.title, agent_type,
                "auto_healed", cumulative_errors[:200], fix_instructions[:200],
            )
            return True, msg

        _log(story.id, agent_type, f"[heal cycle {attempt}] Retry failed: {msg[:300]}", level="error")
        _comm(agent_type, "orchestrator", "heal_outcome", story.id, attempt,
              {"success": False, "error": msg[:500]}, f"Heal failed on cycle {attempt}")
        cumulative_errors += f"\n\n--- Attempt {attempt} error ---\n{msg}"

    return False, cumulative_errors


# ── Smoke Test Helpers ──

_BACKEND_ERROR_PATTERNS = [
    "Traceback", "ModuleNotFoundError", "ImportError", "SyntaxError",
    "NameError", "AttributeError", "TypeError", "ValueError",
    "FileNotFoundError", "RuntimeError", "KeyError",
    "Error loading ASGI app", "Application startup failed",
]

_FRONTEND_ERROR_PATTERNS = [
    "ERROR", "Cannot find module", "Failed to resolve", "SyntaxError",
    "Build failed", "Module not found", "ENOENT", "Could not resolve",
    "Unexpected token", "ReferenceError",
]


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def _scan_for_errors(output: str, patterns: list[str]) -> list[str]:
    found = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for pat in patterns:
            if pat in stripped:
                found.append(stripped)
                break
    return found


def _smoke_test_backend(workspace_root: Path) -> tuple[bool, str]:
    """Start uvicorn on the workspace backend and check for errors."""
    backend_dir = workspace_root / "backend"
    if not (backend_dir / "main.py").exists():
        return True, "No backend main.py found — skipping backend smoke test."

    port = _find_free_port()
    errors: list[str] = []
    proc = None

    try:
        reqs = backend_dir / "requirements.txt"
        if reqs.exists():
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-q", "-r", str(reqs)],
                cwd=backend_dir, capture_output=True, check=False, timeout=60,
            )

        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=backend_dir,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )

        if not _wait_for_port(port, timeout=15):
            stderr = proc.stderr.read() if proc.stderr else ""
            return False, f"Backend failed to start on port {port} within 15s.\nStderr:\n{stderr}"

        time.sleep(1)

        http_ok = False
        http_body = ""
        try:
            resp = urlopen(f"http://127.0.0.1:{port}/", timeout=5)
            http_ok = 200 <= resp.status < 400
            http_body = resp.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            try:
                resp = urlopen(f"http://127.0.0.1:{port}/docs", timeout=5)
                http_ok = 200 <= resp.status < 400
                http_body = "Swagger docs accessible"
            except Exception as exc:
                errors.append(f"HTTP request failed: {exc}")

        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

        stdout = proc.stdout.read() if proc.stdout else ""
        stderr = proc.stderr.read() if proc.stderr else ""
        combined = stdout + "\n" + stderr

        stderr_errors = _scan_for_errors(combined, _BACKEND_ERROR_PATTERNS)
        errors.extend(stderr_errors)

        if not http_ok and not errors:
            errors.append("Backend started but HTTP health check failed.")

        if errors:
            detail = f"Backend smoke test errors (port {port}):\n" + "\n".join(errors)
            detail += f"\n\nFull output:\n{combined[-3000:]}"
            return False, detail

        return True, f"Backend smoke test PASSED (port {port}). {http_body[:100]}"

    except Exception as exc:
        return False, f"Backend smoke test exception: {exc}"
    finally:
        if proc and proc.poll() is None:
            proc.kill()
            proc.wait()


def _smoke_test_frontend(workspace_root: Path) -> tuple[bool, str]:
    """Start vite dev server on the workspace frontend and check for errors."""
    frontend_dir = workspace_root / "frontend"
    if not (frontend_dir / "package.json").exists():
        return True, "No frontend package.json found — skipping frontend smoke test."

    port = _find_free_port()
    errors: list[str] = []
    proc = None

    try:
        subprocess.run(
            ["npm", "install", "--prefer-offline", "--no-audit", "--no-fund"],
            cwd=frontend_dir, capture_output=True, check=False, timeout=120,
        )

        proc = subprocess.Popen(
            ["npx", "vite", "--port", str(port), "--strictPort"],
            cwd=frontend_dir,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )

        if not _wait_for_port(port, timeout=30):
            stderr = proc.stderr.read() if proc.stderr else ""
            stdout = proc.stdout.read() if proc.stdout else ""
            return False, f"Frontend failed to start on port {port} within 30s.\nOutput:\n{stdout}\n{stderr}"

        time.sleep(2)

        http_ok = False
        try:
            resp = urlopen(f"http://127.0.0.1:{port}/", timeout=5)
            http_ok = 200 <= resp.status < 400
        except Exception as exc:
            errors.append(f"Frontend HTTP request failed: {exc}")

        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

        stdout = proc.stdout.read() if proc.stdout else ""
        stderr = proc.stderr.read() if proc.stderr else ""
        combined = stdout + "\n" + stderr

        stderr_errors = _scan_for_errors(combined, _FRONTEND_ERROR_PATTERNS)
        errors.extend(stderr_errors)

        if not http_ok and not errors:
            errors.append("Frontend started but HTTP request to root returned non-2xx.")

        if errors:
            detail = f"Frontend smoke test errors (port {port}):\n" + "\n".join(errors)
            detail += f"\n\nFull output:\n{combined[-3000:]}"
            return False, detail

        return True, f"Frontend smoke test PASSED (port {port})."

    except Exception as exc:
        return False, f"Frontend smoke test exception: {exc}"
    finally:
        if proc and proc.poll() is None:
            proc.kill()
            proc.wait()


def _run_smoke_test(workspace_root: Path) -> tuple[bool, str]:
    """Run backend and frontend smoke tests, return combined result."""
    results: list[str] = []
    all_ok = True

    be_ok, be_msg = _smoke_test_backend(workspace_root)
    results.append(f"--- Backend Smoke Test ---\n{be_msg}")
    if not be_ok:
        all_ok = False

    fe_ok, fe_msg = _smoke_test_frontend(workspace_root)
    results.append(f"--- Frontend Smoke Test ---\n{fe_msg}")
    if not fe_ok:
        all_ok = False

    combined = "\n\n".join(results)
    return all_ok, combined


# ── PM Re-scope ──

def _try_rescope(story: Story, cumulative_errors: str, requirement_text: str,
                 all_stories: list[Story]) -> tuple[str, Story | None]:
    """Ask PM to simplify/skip/halt. Returns (decision, simplified_story_or_None)."""
    _log(story.id, "orchestrator", f"All heal retries exhausted for '{story.title}' — asking PM to re-scope")
    _comm("orchestrator", "pm", "rescope_request", story.id,
          payload={"story_title": story.title}, summary=f"Re-scope '{story.title}'")
    try:
        result = rescope_story(story, cumulative_errors, requirement_text)
        decision = result.get("decision", "halt")
        reason = result.get("reason", "")
        _log(story.id, "pm", f"Re-scope decision: {decision} — {reason}")
        _comm("pm", "orchestrator", "rescope_result", story.id,
              payload=result, summary=f"Decision: {decision}")

        if decision == "simplify" and result.get("simplified_story"):
            simplified_data = result["simplified_story"]
            simplified_data["id"] = story.id
            simplified_data.setdefault("ownership", story.ownership)
            simplified_data.setdefault("dependencies", [])
            simplified_data.setdefault("implementation_notes", [])
            simplified_data.setdefault("test_focus", [])
            simplified_data.setdefault("acceptance_criteria", story.acceptance_criteria)
            simplified_data.setdefault("description", story.description)
            simplified_data.setdefault("title", story.title + " (simplified)")
            return "simplify", Story(**simplified_data)
        return decision, None
    except Exception as exc:
        _log(story.id, "pm", f"Re-scope failed: {exc}", level="error")
        return "halt", None


# ── Main Pipeline ──

def completed_story_ids(pack_id: str) -> set[str]:
    """Story ids whose latest lifecycle event for this pack is story.completed.

    Used by resume runs to skip work that already succeeded (and auto-included
    dependencies that are already done). Failed stories are NOT skipped, so a
    resume naturally retries them.
    """
    try:
        comms = state_store.get_agent_comms(storypack_id=pack_id, limit=2000)
    except Exception:
        return set()
    final: dict[str, str] = {}
    for c in sorted(comms, key=lambda c: c.get("id", 0)):
        et = c.get("event_type")
        if et in ("story.completed", "story.failed"):
            sid = c.get("story_id")
            if sid:
                final[sid] = et
    return {sid for sid, et in final.items() if et == "story.completed"}


def run_agents_background(
    pack_id: str,
    *,
    fast_track: bool | None = None,
    story_ids: list[str] | None = None,
    resume: bool = False,
    include_dependencies: bool = True,
    force: bool = False,
) -> None:
    """Run all agents for an approved storypack.

    Dispatches to the new agentic supervisor (default) unless USE_LEGACY_PIPELINE=1.

    fast_track: when True, skip test phase and smoke (supervisor only). If None,
    reads AGENTIC_FAST_TRACK from the environment.

    story_ids: when set and non-empty, only these stories (plus transitive
    prerequisites per ``Story.dependencies``) are executed. Agents still receive
    the full pack as context via ``Supervisor(all_stories=...)``.

    include_dependencies: when False, run EXACTLY the selected story_ids without
    auto-adding their transitive prerequisites (e.g. to build just the UI shell
    without the backend chain). Un-run dependencies are treated as satisfied by the
    supervisor, so the selected stories still execute.

    resume: when True, stories already completed in a prior run are skipped, so a
    follow-up run only does the pending (and any failed) stories.
    """
    ft = fast_track if fast_track is not None else _env_truthy("AGENTIC_FAST_TRACK")
    if USE_AGENTIC_SUPERVISOR:
        return _run_agents_via_supervisor(
            pack_id, fast_track=ft, story_ids=story_ids, resume=resume,
            include_dependencies=include_dependencies, force=force,
        )
    return _run_agents_legacy(pack_id, story_ids=story_ids, include_dependencies=include_dependencies)


def _run_agents_via_supervisor(
    pack_id: str,
    *,
    fast_track: bool = False,
    story_ids: list[str] | None = None,
    resume: bool = False,
    include_dependencies: bool = True,
    force: bool = False,
) -> None:
    """New agentic runtime: instantiate Supervisor + agents and let them coordinate."""
    global execution_state, _current_run_id, _current_pack_id

    pack = state_store.get_storypack(pack_id)
    if not pack:
        return

    if execution_state.get("running"):
        _log(None, "orchestrator",
             f"Rejected: pipeline already running for {execution_state.get('storypack_id')}",
             level="error")
        return

    run_id = f"run_{uuid.uuid4().hex[:8]}"
    _current_run_id = run_id
    _current_pack_id = pack_id

    full_stories = [Story(**s) for s in pack["stories"]]
    if include_dependencies or not story_ids:
        try:
            stories, auto_included = expand_story_selection(full_stories, story_ids)
        except ValueError as exc:
            _log(None, "orchestrator", f"Invalid story selection: {exc}", level="error")
            return
    else:
        # No-deps: run EXACTLY the selected stories (preserve pack order). Missing
        # prerequisites are NOT added; the supervisor treats un-run deps as satisfied.
        idset = set(story_ids)
        stories = [s for s in full_stories if s.id in idset]
        missing = idset - {s.id for s in stories}
        if missing:
            _log(None, "orchestrator",
                 f"Invalid story selection: unknown ids {sorted(missing)}", level="error")
            return
        auto_included = []
        _log(None, "orchestrator",
             f"No-deps run: {len(stories)} story(ies) exactly, prerequisites NOT auto-included; "
             "acceptance-criteria verification disabled (partial build).")

    # Resume: drop stories already completed in a prior run (including
    # auto-included dependencies that are already done — their artifacts are in
    # the workspace and the supervisor treats a not-rerun dependency as satisfied).
    if resume and not force:
        done = completed_story_ids(pack_id)
        skipped = [s.id for s in stories if s.id in done]
        stories = [s for s in stories if s.id not in done]
        if skipped:
            _log(None, "orchestrator",
                 f"Resume: skipping {len(skipped)} already-completed stories: {', '.join(skipped)}")
        if not stories:
            _log(None, "orchestrator",
                 "Resume: nothing to run — all selected stories are already completed.")
            return
    elif resume and force:
        _log(None, "orchestrator",
             f"Force re-run: running {len(stories)} selected story(ies) including any already completed.")

    requirement_text = pack["requirement_text"]
    workspace_dir = _workspace_for_pack(pack)

    execution_state = {
        "running": True,
        "current_agent": None,
        "current_story_id": None,
        "storypack_id": pack_id,
        "run_id": run_id,
        "completed_stories": [],
        "failed_stories": [],
        "skipped_stories": [],
        "phase": "starting",
        "conclusion": None,
        "runtime": "agentic",
    }

    state_store.update_storypack_status(pack_id, "in_progress")
    subset_note = ""
    if story_ids and len(stories) < len(full_stories):
        subset_note = f" (subset: {len(stories)}/{len(full_stories)} stories"
        if auto_included:
            subset_note += f", auto-included deps: {', '.join(auto_included)}"
        subset_note += ")"
    elif story_ids and auto_included:
        subset_note = (
            f" (subset: {len(stories)} stories; auto-included deps: {', '.join(auto_included)})"
        )

    _log(
        None,
        "orchestrator",
        f"Starting AGENTIC run {run_id} for pack {pack_id}: {len(stories)} stories"
        + (" (fast_track: skip tests + smoke)" if fast_track else "")
        + subset_note,
    )

    try:
        from agents.supervisor import Supervisor, SupervisorConfig

        def _on_progress(agent_id: str, level: str, message: str, detail=None):
            execution_state["current_agent"] = agent_id
            if "story" in (message or "").lower():
                # noisy but informative
                pass

        supervisor = Supervisor(
            storypack_id=pack_id,
            requirement_text=requirement_text,
            stories=stories,
            all_stories=full_stories,
            workspace_dir=workspace_dir,
            config=SupervisorConfig(
                max_pm_heal_attempts=MAX_PM_RETRIES,
                run_tests=(not fast_track)
                and os.getenv("AGENTIC_RUN_TESTS", "1").strip().lower() not in ("0", "false", "no", "off"),
                run_smoke=(not fast_track)
                and os.getenv("AGENTIC_RUN_SMOKE", "1").strip().lower() not in ("0", "false", "no", "off"),
                # No-deps is a partial build (prerequisites intentionally skipped), so
                # acceptance criteria that reference those missing artifacts can't be
                # met. Skip the AC verify gate (and its costly re-run) — the story
                # passes on its own build/DoD instead of burning tokens on an
                # unsatisfiable check.
                verify_acceptance=include_dependencies,
            ),
            on_progress=_on_progress,
        )

        # Expose the live supervisor + budget so /api/agents/metrics can render
        # real-time wall-clock and tool-call usage.
        global _active_supervisor
        _active_supervisor = supervisor
        execution_state["budget"] = supervisor.budget.snapshot()

        result = supervisor.run()

        execution_state["completed_stories"] = [
            {"id": o.story_id, "title": o.title, "summary": o.summary}
            for o in result.outcomes if o.status == "passed"
        ]
        execution_state["failed_stories"] = [
            {"id": o.story_id, "title": o.title, "summary": o.summary}
            for o in result.outcomes if o.status == "failed"
        ]
        execution_state["skipped_stories"] = [
            {"id": o.story_id, "title": o.title, "summary": o.summary}
            for o in result.outcomes if o.status == "skipped"
        ]
        execution_state["conclusion"] = "completed" if result.success else "failed"

        state_store.update_storypack_status(
            pack_id, "completed" if result.success else "failed",
        )
        _log(None, "orchestrator", result.summary,
             level="info" if result.success else "error")
    except Exception as exc:
        _log(None, "orchestrator",
             f"Agentic supervisor crashed: {exc}", level="error")
        state_store.update_storypack_status(pack_id, "failed")
        execution_state["conclusion"] = "failed"
    finally:
        execution_state["phase"] = "done"
        execution_state["running"] = False
        execution_state["current_agent"] = None
        execution_state["current_story_id"] = None
        # Final budget snapshot so the metrics endpoint shows the closing usage,
        # then drop the live reference so subsequent /metrics calls don't
        # report stale wall-clock counters.
        if _active_supervisor is not None and getattr(_active_supervisor, "budget", None):
            try:
                execution_state["budget"] = _active_supervisor.budget.snapshot()
            except Exception:
                pass
        globals()["_active_supervisor"] = None


def _run_agents_legacy(
    pack_id: str, *, story_ids: list[str] | None = None, include_dependencies: bool = True
) -> None:
    """Legacy script-driven orchestration (preserved for safety / regression)."""
    global execution_state, _current_run_id, _current_pack_id

    pack = state_store.get_storypack(pack_id)
    if not pack:
        return

    if execution_state.get("running"):
        _log(None, "orchestrator", f"Rejected: pipeline already running for {execution_state.get('storypack_id')}", level="error")
        return

    run_id = f"run_{uuid.uuid4().hex[:8]}"
    _current_run_id = run_id
    _current_pack_id = pack_id

    full_stories = [Story(**s) for s in pack["stories"]]
    if include_dependencies or not story_ids:
        try:
            stories, auto_included = expand_story_selection(full_stories, story_ids)
        except ValueError as exc:
            _log(None, "orchestrator", f"Invalid story selection: {exc}", level="error")
            return
    else:
        idset = set(story_ids)
        stories = [s for s in full_stories if s.id in idset]
        auto_included = []
    requirement_text = pack["requirement_text"]
    workspace_root = _workspace_for_pack(pack)

    execution_state = {
        "running": True,
        "current_agent": None,
        "current_story_id": None,
        "storypack_id": pack_id,
        "run_id": run_id,
        "completed_stories": [],
        "failed_stories": [],
        "skipped_stories": [],
        "phase": "starting",
        "conclusion": None,
        "runtime": "legacy",
    }

    state_store.update_storypack_status(pack_id, "in_progress")

    backend_stories = _topo_sort_within_phase([s for s in stories if s.ownership == "backend"])
    frontend_stories = _topo_sort_within_phase([s for s in stories if s.ownership == "frontend"])

    subset_note = ""
    if story_ids and auto_included:
        subset_note = f" | auto-included deps: {', '.join(auto_included)}"
    elif story_ids and len(stories) < len(full_stories):
        subset_note = f" | subset {len(stories)}/{len(full_stories)} stories"

    _log(None, "orchestrator",
         f"Starting LEGACY execution run {run_id} for pack {pack_id}: "
         f"{len(backend_stories)} backend, {len(frontend_stories)} frontend"
         f"{subset_note} | "
         f"MAX_PM_RETRIES={MAX_PM_RETRIES}, MAX_TEST_FIX_CYCLES={MAX_TEST_FIX_CYCLES}")

    try:
        _run_pipeline(
            pack_id, stories, requirement_text, backend_stories, frontend_stories,
            all_stories=full_stories,
            workspace_root=workspace_root,
        )
    except Exception as exc:
        _log(None, "orchestrator", f"Pipeline crashed with unhandled exception: {exc}", level="error")
        state_store.update_storypack_status(pack_id, "failed")
        execution_state["conclusion"] = "failed"
    finally:
        execution_state["phase"] = "done"
        execution_state["running"] = False
        execution_state["current_agent"] = None
        execution_state["current_story_id"] = None


def _run_pipeline(
    pack_id: str,
    run_stories: list,
    requirement_text: str,
    backend_stories: list,
    frontend_stories: list,
    *,
    all_stories: list | None = None,
    workspace_root: Path,
) -> None:
    """Inner pipeline logic — exceptions propagate to run_agents_background."""
    context_stories = all_stories if all_stories is not None else run_stories
    halted = False

    # ── Backend Phase ──
    execution_state["phase"] = "backend"
    for story in backend_stories:
        execution_state["current_agent"] = "backend"
        execution_state["current_story_id"] = story.id
        _log(story.id, "backend", f"Starting: {story.title}")
        _comm("orchestrator", "backend", "story_assignment", story.id,
              summary=f"Assigned '{story.title}' to backend")

        progress = _make_progress_cb(story.id, "backend")
        ok, msg = implement_backend(
            story, requirement_text, context_stories, on_progress=progress, workspace_root=workspace_root,
        )
        _comm("backend", "orchestrator", "build_result", story.id,
              payload={"success": ok, "message": msg[:500]},
              summary=f"{'Success' if ok else 'Failed'}: {story.title}")

        if ok:
            execution_state["completed_stories"].append(story.id)
            _log(story.id, "backend", f"Completed: {msg}")
        else:
            ok, msg = _heal_story(
                story, "backend", implement_backend, requirement_text, context_stories, msg,
                workspace_root=workspace_root,
            )
            if ok:
                execution_state["completed_stories"].append(story.id)
            else:
                decision, simplified = _try_rescope(story, msg, requirement_text, context_stories)
                if decision == "simplify" and simplified:
                    _log(story.id, "orchestrator", f"Re-trying with simplified story: {simplified.title}")
                    progress = _make_progress_cb(story.id, "backend")
                    ok2, msg2 = implement_backend(
                        simplified, requirement_text, context_stories, on_progress=progress,
                        workspace_root=workspace_root,
                    )
                    if ok2:
                        execution_state["completed_stories"].append(story.id)
                        _log(story.id, "backend", f"Simplified story succeeded: {msg2}")
                    else:
                        execution_state["failed_stories"].append(story.id)
                        _log(story.id, "orchestrator", f"Simplified story also failed — halting", level="error")
                        halted = True
                        break
                elif decision == "skip":
                    execution_state["skipped_stories"].append(story.id)
                    _log(story.id, "orchestrator", f"PM decided to skip '{story.title}'")
                else:
                    execution_state["failed_stories"].append(story.id)
                    _log(None, "orchestrator", f"Pipeline HALTED — '{story.title}' unrecoverable", level="error")
                    halted = True
                    break

    # ── Publish API Contract ──
    if not halted and execution_state["completed_stories"]:
        _publish_contract(workspace_root)

    # ── Frontend Phase ──
    if not halted:
        execution_state["phase"] = "frontend"
        for story in frontend_stories:
            execution_state["current_agent"] = "frontend"
            execution_state["current_story_id"] = story.id
            _log(story.id, "frontend", f"Starting: {story.title}")
            _comm("orchestrator", "frontend", "story_assignment", story.id,
                  summary=f"Assigned '{story.title}' to frontend")

            progress = _make_progress_cb(story.id, "frontend")
            ok, msg = implement_frontend(
                story, requirement_text, context_stories, on_progress=progress, workspace_root=workspace_root,
            )
            _comm("frontend", "orchestrator", "build_result", story.id,
                  payload={"success": ok, "message": msg[:500]},
                  summary=f"{'Success' if ok else 'Failed'}: {story.title}")

            if ok:
                execution_state["completed_stories"].append(story.id)
                _log(story.id, "frontend", f"Completed: {msg}")
            else:
                ok, msg = _heal_story(
                    story, "frontend", implement_frontend, requirement_text, context_stories, msg,
                    workspace_root=workspace_root,
                )
                if ok:
                    execution_state["completed_stories"].append(story.id)
                else:
                    decision, simplified = _try_rescope(story, msg, requirement_text, context_stories)
                    if decision == "simplify" and simplified:
                        _log(story.id, "orchestrator", f"Re-trying with simplified story: {simplified.title}")
                        progress = _make_progress_cb(story.id, "frontend")
                        ok2, msg2 = implement_frontend(
                            simplified, requirement_text, context_stories, on_progress=progress,
                            workspace_root=workspace_root,
                        )
                        if ok2:
                            execution_state["completed_stories"].append(story.id)
                            _log(story.id, "frontend", f"Simplified story succeeded: {msg2}")
                        else:
                            execution_state["failed_stories"].append(story.id)
                            _log(story.id, "orchestrator", f"Simplified story also failed — halting", level="error")
                            halted = True
                            break
                    elif decision == "skip":
                        execution_state["skipped_stories"].append(story.id)
                        _log(story.id, "orchestrator", f"PM decided to skip '{story.title}'")
                    else:
                        execution_state["failed_stories"].append(story.id)
                        _log(None, "orchestrator", f"Pipeline HALTED — '{story.title}' unrecoverable", level="error")
                        halted = True
                        break

    # ── Test-Fix-Retest Phase ──
    tests_passed = False
    if not halted:
        execution_state["phase"] = "testing"
        execution_state["current_agent"] = "testing"
        execution_state["current_story_id"] = None
        _log(None, "testing", "Starting test generation and execution")

        progress = _make_progress_cb(None, "testing")
        ok, msg = implement_tests(
            context_stories, requirement_text, on_progress=progress, workspace_root=workspace_root,
        )

        if ok:
            _log(None, "testing", f"Tests passed: {msg}")
            state_store.save_test_result(pack_id, "all", True, msg)
            tests_passed = True
        else:
            _log(None, "testing", f"Tests failed on initial run: {msg}", level="error")
            state_store.save_test_result(pack_id, "all", False, msg)

            for cycle in range(1, MAX_TEST_FIX_CYCLES + 1):
                _log(None, "orchestrator", f"[test-fix cycle {cycle}/{MAX_TEST_FIX_CYCLES}] PM triaging test failure")
                _comm("orchestrator", "pm", "test_triage_request", cycle=cycle,
                      payload={"test_output": msg[:2000]},
                      summary=f"Test-fix cycle {cycle}")

                try:
                    triage = triage_test_failure(msg, context_stories, requirement_text)
                    target_agent = triage.get("target_agent", "testing")
                    root_cause = triage.get("root_cause", "unknown")
                    fix_instructions = triage.get("fix_instructions", "")
                    responsible_id = triage.get("responsible_story_id", "unknown")

                    _log(None, "pm", f"[test-fix {cycle}] Triage: target={target_agent}, story={responsible_id}",
                         detail=f"Root cause: {root_cause}\nFix: {fix_instructions}")
                    _comm("pm", "orchestrator", "test_triage_result", cycle=cycle,
                          payload=triage, summary=f"Target: {target_agent}, cause: {root_cause[:100]}")
                except Exception as exc:
                    _log(None, "pm", f"PM triage error: {exc}", level="error")
                    target_agent = "testing"
                    fix_instructions = f"PM triage failed ({exc}). Test output:\n{msg}"
                    responsible_id = "unknown"

                fix_context = f"Test failure output:\n{msg[:3000]}\n\nPM fix instructions:\n{fix_instructions}"

                if target_agent in ("backend", "frontend"):
                    target_story = next(
                        (s for s in context_stories if s.id == responsible_id),
                        next((s for s in context_stories if s.ownership == target_agent), None),
                    )
                    if target_story:
                        impl_fn = implement_backend if target_agent == "backend" else implement_frontend
                        execution_state["current_agent"] = target_agent
                        execution_state["current_story_id"] = target_story.id
                        _log(target_story.id, "orchestrator",
                             f"[test-fix {cycle}] Routing fix to {target_agent} for '{target_story.title}'")
                        _comm("orchestrator", target_agent, "test_fix_route", target_story.id, cycle,
                              summary=f"Fix '{target_story.title}' based on test failure")

                        fix_ok, fix_msg = _heal_story(
                            target_story, target_agent, impl_fn,
                            requirement_text, context_stories, fix_context,
                            workspace_root=workspace_root,
                        )
                else:
                    _log(None, "orchestrator", f"[test-fix {cycle}] PM says bug is in tests — re-generating")

                _log(None, "testing", f"[test-fix {cycle}/{MAX_TEST_FIX_CYCLES}] Re-running tests")
                execution_state["current_agent"] = "testing"
                execution_state["current_story_id"] = None
                ok, msg = implement_tests(
                    context_stories, requirement_text, on_progress=progress, fix_context=fix_context,
                    workspace_root=workspace_root,
                )
                state_store.save_test_result(pack_id, "all", ok, msg)

                if ok:
                    _log(None, "orchestrator", f"[test-fix {cycle}] Tests PASSED after fix")
                    tests_passed = True
                    break
                else:
                    _log(None, "testing", f"[test-fix {cycle}] Tests still failing: {msg[:300]}", level="error")

            if not tests_passed:
                _log(None, "orchestrator", f"Tests remain failing after {MAX_TEST_FIX_CYCLES} fix cycles", level="error")
    else:
        _log(None, "orchestrator", "Skipped testing phase due to pipeline halt", level="error")

    # ── Smoke Test Phase ──
    smoke_passed = False
    if not halted:
        execution_state["phase"] = "smoke_test"
        execution_state["current_agent"] = "orchestrator"
        execution_state["current_story_id"] = None
        _log(None, "orchestrator", "Starting integration smoke test — booting backend and frontend servers")
        _comm("orchestrator", "orchestrator", "smoke_test_start",
              summary="Integration smoke test starting")

        smoke_ok, smoke_msg = _run_smoke_test(workspace_root)
        _log(None, "orchestrator",
             f"Smoke test initial result: {'PASSED' if smoke_ok else 'FAILED'}",
             detail=smoke_msg)

        if smoke_ok:
            smoke_passed = True
            _comm("orchestrator", "orchestrator", "smoke_test_result",
                  payload={"passed": True}, summary="Smoke test PASSED")
        else:
            _comm("orchestrator", "orchestrator", "smoke_test_result",
                  payload={"passed": False, "errors": smoke_msg[:2000]},
                  summary="Smoke test FAILED — entering fix loop")

            for smoke_cycle in range(1, MAX_SMOKE_FIX_CYCLES + 1):
                _log(None, "orchestrator",
                     f"[smoke-fix {smoke_cycle}/{MAX_SMOKE_FIX_CYCLES}] PM triaging smoke test errors")
                _comm("orchestrator", "pm", "smoke_fix_request", cycle=smoke_cycle,
                      payload={"smoke_errors": smoke_msg[:2000]},
                      summary=f"Smoke-fix cycle {smoke_cycle}")

                target_agent = "backend"
                fix_instructions = ""
                responsible_id = "unknown"
                try:
                    triage = triage_test_failure(smoke_msg, context_stories, requirement_text)
                    target_agent = triage.get("target_agent", "backend")
                    root_cause = triage.get("root_cause", "unknown")
                    fix_instructions = triage.get("fix_instructions", "")
                    responsible_id = triage.get("responsible_story_id", "unknown")

                    _log(None, "pm",
                         f"[smoke-fix {smoke_cycle}] Triage: target={target_agent}, story={responsible_id}",
                         detail=f"Root cause: {root_cause}\nFix: {fix_instructions}")
                    _comm("pm", "orchestrator", "smoke_triage_result", cycle=smoke_cycle,
                          payload=triage, summary=f"Target: {target_agent}, cause: {root_cause[:100]}")
                except Exception as exc:
                    _log(None, "pm", f"PM smoke triage error: {exc}", level="error")
                    fix_instructions = f"PM triage failed ({exc}). Smoke errors:\n{smoke_msg}"

                fix_context = (
                    f"Integration smoke test failure (servers failed to start cleanly):\n"
                    f"{smoke_msg[:3000]}\n\nPM fix instructions:\n{fix_instructions}"
                )

                if target_agent in ("backend", "frontend"):
                    target_story = next(
                        (s for s in context_stories if s.id == responsible_id),
                        next((s for s in context_stories if s.ownership == target_agent), None),
                    )
                    if target_story:
                        impl_fn = implement_backend if target_agent == "backend" else implement_frontend
                        execution_state["current_agent"] = target_agent
                        execution_state["current_story_id"] = target_story.id
                        _log(target_story.id, "orchestrator",
                             f"[smoke-fix {smoke_cycle}] Routing fix to {target_agent} for '{target_story.title}'")
                        _comm("orchestrator", target_agent, "smoke_fix_route",
                              target_story.id, smoke_cycle,
                              summary=f"Fix '{target_story.title}' from smoke test failure")

                        fix_ok, fix_msg = _heal_story(
                            target_story, target_agent, impl_fn,
                            requirement_text, context_stories, fix_context,
                            workspace_root=workspace_root,
                        )
                        if fix_ok:
                            _log(target_story.id, target_agent,
                                 f"[smoke-fix {smoke_cycle}] Agent fix succeeded")
                        else:
                            _log(target_story.id, target_agent,
                                 f"[smoke-fix {smoke_cycle}] Agent fix failed", level="error")

                _log(None, "orchestrator",
                     f"[smoke-fix {smoke_cycle}/{MAX_SMOKE_FIX_CYCLES}] Re-running smoke test")
                execution_state["current_agent"] = "orchestrator"
                execution_state["current_story_id"] = None

                smoke_ok, smoke_msg = _run_smoke_test(workspace_root)
                _log(None, "orchestrator",
                     f"[smoke-fix {smoke_cycle}] Smoke re-test: {'PASSED' if smoke_ok else 'FAILED'}",
                     detail=smoke_msg)
                _comm("orchestrator", "orchestrator", "smoke_fix_result",
                      cycle=smoke_cycle,
                      payload={"passed": smoke_ok},
                      summary=f"Smoke re-test cycle {smoke_cycle}: {'PASSED' if smoke_ok else 'FAILED'}")

                if smoke_ok:
                    smoke_passed = True
                    break

            if not smoke_passed:
                _log(None, "orchestrator",
                     f"Smoke test still failing after {MAX_SMOKE_FIX_CYCLES} fix cycles",
                     level="error")
    else:
        _log(None, "orchestrator", "Skipped smoke test phase due to pipeline halt", level="error")

    # ── Conclusion ──
    skipped = execution_state["skipped_stories"]
    failed = execution_state["failed_stories"]

    if halted or failed:
        conclusion = "failed"
        state_store.update_storypack_status(pack_id, "failed")
    elif not smoke_passed and not halted:
        conclusion = "failed"
        state_store.update_storypack_status(pack_id, "failed")
    elif skipped and not tests_passed:
        conclusion = "partial"
        state_store.update_storypack_status(pack_id, "completed")
    elif tests_passed and smoke_passed:
        conclusion = "completed"
        state_store.update_storypack_status(pack_id, "completed")
    else:
        conclusion = "completed"
        state_store.update_storypack_status(pack_id, "completed")

    execution_state["conclusion"] = conclusion
    _log(None, "orchestrator",
         f"Execution complete for pack {pack_id} — conclusion: {conclusion} "
         f"(completed={len(execution_state['completed_stories'])}, "
         f"failed={len(failed)}, skipped={len(skipped)}, "
         f"smoke_test={'PASSED' if smoke_passed else 'FAILED'})")


# ── Manual Fix ──

fix_state: dict = {
    "running": False,
    "fix_id": None,
    "agent_type": None,
    "story_id": None,
}


def run_fix_background(fix_id: str) -> None:
    """Re-run a single agent for one story with error context from a fix request."""
    global fix_state

    fix_req = state_store.get_fix_request(fix_id)
    if not fix_req:
        return

    pack = state_store.get_storypack(fix_req["storypack_id"])
    if not pack:
        state_store.update_fix_request_status(fix_id, "failed", "Storypack not found")
        return

    stories = [Story(**s) for s in pack["stories"]]
    target_story = next((s for s in stories if s.id == fix_req["story_id"]), None)
    if not target_story:
        state_store.update_fix_request_status(fix_id, "failed", "Story not found in pack")
        return

    fix_context = f"Previous attempt failed with this error:\n{fix_req['error_text']}"
    if fix_req.get("user_instructions"):
        fix_context += f"\n\nUser instructions for fix:\n{fix_req['user_instructions']}"
    fix_context += "\n\nFix the implementation. Preserve working code."

    agent_type = fix_req["agent_type"]
    requirement_text = pack["requirement_text"]
    workspace_root = _workspace_for_pack(pack)

    fix_state = {
        "running": True,
        "fix_id": fix_id,
        "agent_type": agent_type,
        "story_id": fix_req["story_id"],
    }

    state_store.update_fix_request_status(fix_id, "running")
    _log(fix_req["story_id"], agent_type, f"Fix attempt started (#{fix_req['attempt_number']})")
    progress = _make_progress_cb(fix_req["story_id"], agent_type)

    try:
        if agent_type == "backend":
            ok, msg = implement_backend(
                target_story, requirement_text, stories,
                on_progress=progress, fix_context=fix_context, workspace_root=workspace_root,
            )
        elif agent_type == "frontend":
            ok, msg = implement_frontend(
                target_story, requirement_text, stories,
                on_progress=progress, fix_context=fix_context, workspace_root=workspace_root,
            )
        elif agent_type == "testing":
            ok, msg = implement_tests(
                stories, requirement_text,
                on_progress=progress, fix_context=fix_context, workspace_root=workspace_root,
            )
        else:
            ok, msg = False, f"Unknown agent type: {agent_type}"

        if ok:
            state_store.update_fix_request_status(fix_id, "success", msg)
            _log(fix_req["story_id"], agent_type, f"Fix succeeded: {msg}")
        else:
            state_store.update_fix_request_status(fix_id, "failed", msg)
            _log(fix_req["story_id"], agent_type, f"Fix failed: {msg}", level="error")
    except Exception as exc:
        state_store.update_fix_request_status(fix_id, "failed", str(exc))
        _log(fix_req["story_id"], agent_type, f"Fix error: {exc}", level="error")
    finally:
        fix_state = {
            "running": False,
            "fix_id": None,
            "agent_type": None,
            "story_id": None,
        }


# ── Enhancement Request ──

enhance_state: dict = {
    "running": False,
    "enhance_id": None,
    "agent_type": None,
    "phase": "idle",
}


def _set_enhance_state(running: bool, enhance_id, agent_type, phase: str) -> None:
    """Update the shared enhancement state IN PLACE.

    Rebinding the module global (``enhance_state = {...}``) would leave the
    reference imported by routes/enhancements.py pointing at a stale dict, so the
    single-flight guard there would read the wrong ``running`` flag. Mutating in
    place keeps every holder of the dict consistent.
    """
    enhance_state.clear()
    enhance_state.update({
        "running": running,
        "enhance_id": enhance_id,
        "agent_type": agent_type,
        "phase": phase,
    })

# Standalone test-run state (single-flight, mirrors enhance_state/fix_state).
test_run_state: dict = {
    "running": False,
    "project_id": None,
    "run_id": None,
    "phase": "idle",
    "success": None,
    "summary": "",
}


def run_tests_background(project_id: str = "default") -> None:
    """Run the Test Agent standalone against an existing workspace (dashboard 'Run
    tests' action). Records the run into the per-POC ledger with trigger='manual'."""
    global test_run_state, _current_run_id
    import uuid as _uuid
    from agents.test_runner import run_tests_for_project

    run_id = f"testrun_{_uuid.uuid4().hex[:8]}"
    _current_run_id = run_id
    test_run_state = {
        "running": True, "project_id": project_id, "run_id": run_id,
        "phase": "testing", "success": None, "summary": "",
    }

    def _progress(level, message, detail=None):
        _log(None, "test", message, level=level, detail=detail)

    try:
        res = run_tests_for_project(
            project_id, run_id=run_id, trigger="manual", on_progress=_progress)
        test_run_state.update(
            running=False, phase="done",
            success=res.get("success"), summary=res.get("summary", ""))
    except Exception as exc:  # noqa: BLE001
        _log(None, "test", f"Standalone test run failed: {exc}", level="error")
        test_run_state.update(running=False, phase="error", success=False, summary=str(exc))

MAX_ENHANCE_RETRIES = 2


def _build_enhancement_context(description: str, context: str, workspace_root: Path) -> str:
    """Build the shared workspace context string for enhancement mode."""
    workspace_context = ""
    scope_path = workspace_root / "scope.json"
    if scope_path.exists():
        workspace_context += f"\nExisting scope.json:\n{scope_path.read_text()[:2000]}"
    contract_path = workspace_root / "contracts" / "api_contract.json"
    if contract_path.exists():
        workspace_context += f"\nExisting API contract:\n{contract_path.read_text()[:2000]}"

    enhancement_context = (
        "ENHANCEMENT MODE: You are modifying an EXISTING codebase, not starting fresh.\n"
        "Read the existing files in the workspace directory and modify/extend them.\n"
        "Do NOT recreate files from scratch — update what already exists.\n"
        f"\nUser enhancement request:\n{description}\n"
    )
    if context:
        enhancement_context += f"\nAdditional context:\n{context}\n"
    enhancement_context += workspace_context
    return enhancement_context


def _run_enhance_agentic(
    story: Story, agent_type: str, description: str, all_stories: list, workspace_root: Path,
) -> tuple[bool, str]:
    """Run an enhancement through the agentic ReAct agent instead of the legacy
    plan->patch path. Gains: the full iter/tool-result log stream, the design-system
    skill (professional register), and a DoD-gated promote (build + check_ui). The
    agent self-heals internally, so no outer PM heal loop is needed here.
    """
    from agents.agentic.base import RunContext, Budget
    from agents.agentic.frontend import FrontendAgent
    from agents.agentic.backend import BackendAgent
    from agents.bus import MessageBus

    bus = MessageBus()
    ctx = RunContext(
        storypack_id=_current_pack_id or f"enhance-{story.id}",
        run_id=_current_run_id or f"enhance-run-{story.id}",
        workspace_dir=workspace_root,
        bus=bus,
        budget=Budget.from_env(),
        requirement_text=description,
        all_stories=list(all_stories) if all_stories else [story],
    )

    def _progress(level, message, detail=None):
        _log(story.id, agent_type, message, level=level, detail=detail)

    AgentCls = BackendAgent if agent_type == "backend" else FrontendAgent
    agent = AgentCls(ctx, on_progress=_progress)
    agent.register_with_bus()
    try:
        outcome = agent.run(story)
    finally:
        try:
            agent.unregister()
        except Exception:  # noqa: BLE001
            pass

    if outcome.success:
        _log(story.id, agent_type, f"Enhancement implemented (agentic): {(outcome.summary or '')[:200]}")
        return True, outcome.summary or outcome.final_text or "ok"
    return False, outcome.summary or outcome.error or "Enhancement failed"


def _run_single_enhance_agent(
    story: Story,
    agent_type: str,
    enhancement_context: str,
    description: str,
    all_stories: list,
    workspace_root: Path,
) -> tuple[bool, str]:
    """Implement one enhancement story. With AGENTIC_ENHANCE set, route through the
    agentic ReAct agent (rich logs + design skill + DoD gate); otherwise use the legacy
    plan->patch path + PM heal loop."""
    if _env_truthy("AGENTIC_ENHANCE"):
        return _run_enhance_agentic(story, agent_type, description, all_stories, workspace_root)

    progress = _make_progress_cb(story.id, agent_type)
    impl_fn = implement_backend if agent_type == "backend" else implement_frontend

    ok, msg = impl_fn(
        story, enhancement_context, all_stories, on_progress=progress,
        workspace_root=workspace_root,
    )

    if ok:
        _log(story.id, agent_type, f"Enhancement implemented: {msg}")
        return True, msg

    _log(story.id, agent_type, f"Enhancement failed, entering heal loop: {msg[:300]}", level="error")
    enhance_state["phase"] = f"healing_{agent_type}"

    cumulative_errors = msg
    for attempt in range(1, MAX_ENHANCE_RETRIES + 1):
        _log(story.id, "orchestrator",
             f"[enhance-heal {attempt}/{MAX_ENHANCE_RETRIES}] PM analyzing {agent_type} failure")
        try:
            fix_instructions = analyze_failure(story, cumulative_errors, description)
            _log(story.id, "pm", "Fix instructions ready", detail=fix_instructions)
        except Exception as exc:
            fix_instructions = f"PM analysis failed: {exc}. Error:\n{cumulative_errors}"
            _log(story.id, "pm", f"PM analysis error: {exc}", level="error")

        _log(story.id, agent_type, f"[enhance-heal {attempt}] Retrying with PM guidance")
        ok2, msg2 = impl_fn(
            story, enhancement_context, all_stories,
            on_progress=progress, fix_context=fix_instructions,
            workspace_root=workspace_root,
        )

        if ok2:
            _log(story.id, agent_type, f"[enhance-heal {attempt}] Succeeded: {msg2}")
            return True, msg2
        else:
            _log(story.id, agent_type, f"[enhance-heal {attempt}] Still failing: {msg2[:300]}", level="error")
            cumulative_errors += f"\n\n--- Attempt {attempt} ---\n{msg2}"

    return False, f"Failed after {MAX_ENHANCE_RETRIES} heal retries: {cumulative_errors[-500:]}"


def _enhance_auto_apply() -> bool:
    """When set, skip BOTH review gates (plan + diff) and apply immediately
    (restores the pre-gate one-shot behavior for automation/CLI callers)."""
    return os.getenv("ENHANCE_AUTO_APPLY", "").strip().lower() in ("1", "true", "yes")


# ── Enhancement diff gate (Phase 2) ──

# Directories that never carry reviewable source (regenerated by build/install).
_DIFF_IGNORE_DIRS = {"node_modules", ".venv", "__pycache__", "dist", "build", ".git", ".pytest_cache"}
# Extensions we render as a unified text diff. Anything else is reported by
# status only (added/modified/deleted) with no body.
_DIFF_TEXT_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".css", ".scss", ".html",
    ".md", ".txt", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".env", ".sh",
}
_DIFF_MAX_FILE_BYTES = 300_000   # skip diffing files larger than this
_DIFF_MAX_LINES_PER_FILE = 600   # truncate a single file's unified diff


def _iter_project_files(root: Path) -> dict:
    """Map relpath -> Path for reviewable files under _BACKUP_SUBDIRS + _BACKUP_FILES."""
    from agents.reasoning import _BACKUP_SUBDIRS, _BACKUP_FILES

    out: dict = {}
    if not root or not root.exists():
        return out
    for subdir_name in _BACKUP_SUBDIRS:
        base = root / subdir_name
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if p.is_dir():
                continue
            if any(part in _DIFF_IGNORE_DIRS for part in p.relative_to(root).parts):
                continue
            out[str(p.relative_to(root))] = p
    for file_name in _BACKUP_FILES:
        f = root / file_name
        if f.is_file():
            out[file_name] = f
    return out


def _read_lines(path: Path) -> list[str] | None:
    try:
        if path.stat().st_size > _DIFF_MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return None


def compute_enhancement_diff(live_dir: Path, staging_dir: Path) -> list[dict]:
    """Compare the live workspace with a staged copy and return one entry per
    changed file: {path, status: added|modified|deleted, additions, deletions, diff}.

    Read-only: touches neither tree. ``diff`` is a unified diff for text files
    (truncated), or "" for binary/oversized files.
    """
    live = _iter_project_files(live_dir)
    staged = _iter_project_files(staging_dir)
    results: list[dict] = []

    for rel in sorted(set(live) | set(staged)):
        lp = live.get(rel)
        sp = staged.get(rel)
        if lp and not sp:
            status = "deleted"
        elif sp and not lp:
            status = "added"
        else:
            try:
                if lp.stat().st_size == sp.stat().st_size and lp.read_bytes() == sp.read_bytes():
                    continue  # identical
            except OSError:
                continue
            status = "modified"

        ext = Path(rel).suffix.lower()
        diff_text = ""
        additions = deletions = 0
        if ext in _DIFF_TEXT_EXT:
            before = _read_lines(lp) if lp else []
            after = _read_lines(sp) if sp else []
            if before is None or after is None:
                diff_text = "(binary or oversized file — not shown)"
            else:
                lines = list(difflib.unified_diff(
                    before or [], after or [],
                    fromfile=f"live/{rel}", tofile=f"staged/{rel}",
                ))
                for ln in lines:
                    if ln.startswith("+") and not ln.startswith("+++"):
                        additions += 1
                    elif ln.startswith("-") and not ln.startswith("---"):
                        deletions += 1
                if len(lines) > _DIFF_MAX_LINES_PER_FILE:
                    lines = lines[:_DIFF_MAX_LINES_PER_FILE] + [f"\n… diff truncated ({len(lines)} lines total)\n"]
                diff_text = "".join(lines)
        else:
            diff_text = "(no text diff for this file type)"

        results.append({
            "path": rel,
            "status": status,
            "additions": additions,
            "deletions": deletions,
            "diff": diff_text,
        })
    return results


def get_enhancement_diff(enhance_id: str) -> dict:
    """Serve the diff for a staged enhancement (recomputed live from staging_path)."""
    enh = state_store.get_enhancement(enhance_id)
    if not enh:
        return {"enhance_id": enhance_id, "found": False, "files": []}
    staging_path = enh.get("staging_path") or ""
    if not staging_path or not Path(staging_path).exists():
        return {"enhance_id": enhance_id, "found": False, "files": [],
                "note": "No staged changes available (already promoted, discarded, or never staged)."}
    live_dir = resolve_workspace_dir(enh.get("project_id"))
    files = compute_enhancement_diff(live_dir, Path(staging_path))
    return {"enhance_id": enhance_id, "found": True, "files": files, "count": len(files)}


def plan_enhancement_background(enhance_id: str) -> None:
    """Phase 1 (review gate): PM turns the request into a plan, then STOP at
    ``pending_review`` so a human can approve/reject before any code changes.

    No workspace backup and no agents run here — nothing is touched until the
    plan is approved via ``apply_enhancement_background``.
    """
    enh = state_store.get_enhancement(enhance_id)
    if not enh:
        return

    agent_type = enh["agent_type"]
    description = enh["description"]
    context = enh.get("context", "")

    _set_enhance_state(True, enhance_id, agent_type, "planning")
    state_store.update_enhancement_status(enhance_id, "planning")
    _log(None, "pm", f"Enhancement {enhance_id} — PM planning {agent_type} change(s)")

    try:
        if agent_type == "both":
            stories_data = create_enhancement_stories_both(description, context)
        else:
            stories_data = [create_enhancement_story(agent_type, description, context)]

        # Validate the plan parses into Story objects before offering it for review.
        _ = [Story(**sd) for sd in stories_data]

        state_store.update_enhancement_status(
            enhance_id, "pending_review",
            story_json=json.dumps(stories_data, default=str),
        )
        for sd in stories_data:
            _log(None, "pm", f"Planned story: {sd.get('title')} ({sd.get('ownership')})")
        _log(None, "orchestrator",
             f"Enhancement {enhance_id} plan ready — awaiting review before any changes")
    except Exception as exc:
        _log(None, "orchestrator", f"Enhancement {enhance_id} planning failed: {exc}", level="error")
        state_store.update_enhancement_status(enhance_id, "failed", f"Planning failed: {exc}")
        return
    finally:
        _set_enhance_state(False, None, None, "idle")

    if _enhance_auto_apply():
        _log(None, "orchestrator", f"ENHANCE_AUTO_APPLY set — applying plan {enhance_id} without review")
        apply_enhancement_background(enhance_id)


def apply_enhancement_background(enhance_id: str) -> None:
    """Phase 2 (diff gate): run an approved plan into an ISOLATED STAGING COPY of
    the workspace — the live workspace is never touched here. On success, stop at
    ``pending_promote`` with a reviewable diff; the changes only land in the live
    workspace when ``promote_enhancement`` is called.
    """
    global _current_pack_id, _current_run_id

    enh = state_store.get_enhancement(enhance_id)
    if not enh:
        return

    agent_type = enh["agent_type"]
    description = enh["description"]
    context = enh.get("context", "")

    # Source stories from the approved plan, not a fresh PM call, so what runs is
    # exactly what was reviewed.
    try:
        parsed = json.loads(enh.get("story_json") or "[]")
        raw_stories = parsed if isinstance(parsed, list) else [parsed]
        stories = [Story(**sd) for sd in raw_stories if isinstance(sd, dict) and sd]
    except Exception as exc:
        state_store.update_enhancement_status(enhance_id, "failed", f"Invalid approved plan: {exc}")
        return
    if not stories:
        state_store.update_enhancement_status(enhance_id, "failed", "No approved plan to apply.")
        return

    _current_pack_id = f"enhance-{enhance_id}"
    _current_run_id = f"enhance-run-{enhance_id}"

    _set_enhance_state(True, enhance_id, agent_type, "staging")

    state_store.update_enhancement_status(enhance_id, "running")
    _log(None, "orchestrator", f"Enhancement {enhance_id} approved — running into staging copy")

    live_root = resolve_workspace_dir(enh.get("project_id"))

    # Build a staging copy and run the whole enhancement into it. Everything the
    # agents do (scratch, promote, boot, npm/pytest) is confined to staging.
    try:
        staging_root = Path(stage_workspace(live_root, enhance_id))
        state_store.set_enhancement_staging(enhance_id, str(staging_root))
        _log(None, "orchestrator", f"Staging copy created at {staging_root}")
    except Exception as exc:
        _log(None, "orchestrator", f"Failed to create staging copy: {exc}", level="error")
        state_store.update_enhancement_status(enhance_id, "failed", f"Staging failed: {exc}")
        _set_enhance_state(False, None, None, "idle")
        return

    all_ok = True
    summary = ""
    try:
        enhancement_context = _build_enhancement_context(description, context, staging_root)

        if agent_type == "both":
            for s in stories:
                _log(None, "pm", f"Enhancement story: {s.title} ({s.ownership})")

            be_stories = [s for s in stories if s.ownership == "backend"]
            fe_stories = [s for s in stories if s.ownership == "frontend"]
            results: list[str] = []

            for story in be_stories:
                enhance_state["phase"] = "backend"
                enhance_state["agent_type"] = "backend"
                _log(story.id, "backend", f"Starting backend enhancement: {story.title}")
                ok, msg = _run_single_enhance_agent(
                    story, "backend", enhancement_context, description, stories, staging_root,
                )
                results.append(f"backend ({story.title}): {'OK' if ok else 'FAILED'}")
                if not ok:
                    all_ok = False
                    break

            if all_ok:
                _publish_contract(staging_root)
                for story in fe_stories:
                    enhance_state["phase"] = "frontend"
                    enhance_state["agent_type"] = "frontend"
                    _log(story.id, "frontend", f"Starting frontend enhancement: {story.title}")
                    ok, msg = _run_single_enhance_agent(
                        story, "frontend", enhancement_context, description, stories, staging_root,
                    )
                    results.append(f"frontend ({story.title}): {'OK' if ok else 'FAILED'}")
                    if not ok:
                        all_ok = False
                        break

            summary = " | ".join(results)

        else:
            story = stories[0]
            _log(None, "pm", f"Applying enhancement story: {story.title}",
                 detail=json.dumps(story.model_dump(), indent=2, default=str))
            enhance_state["phase"] = "implementing"
            ok, summary = _run_single_enhance_agent(
                story, agent_type, enhancement_context, description, [story], staging_root,
            )
            all_ok = ok

    except Exception as exc:
        _log(None, "orchestrator", f"Enhancement {enhance_id} crashed: {exc}", level="error")
        all_ok = False
        summary = str(exc)

    # Post-run: succeed -> hold at diff gate; fail -> discard staging.
    try:
        if all_ok:
            diff_files = compute_enhancement_diff(live_root, staging_root)
            n = len(diff_files)
            state_store.update_enhancement_status(
                enhance_id, "pending_promote",
                f"{summary}  [{n} file(s) changed — review the diff, then promote or discard]",
            )
            _log(None, "orchestrator",
                 f"Enhancement {enhance_id} staged: {n} file(s) changed — awaiting promote/discard")
        else:
            discard_staging(str(staging_root))
            state_store.set_enhancement_staging(enhance_id, "")
            state_store.update_enhancement_status(enhance_id, "failed", summary)
            _log(None, "orchestrator", f"Enhancement {enhance_id} failed in staging — staging discarded")
    finally:
        _set_enhance_state(False, None, None, "idle")

    if all_ok and _enhance_auto_apply():
        _log(None, "orchestrator", f"ENHANCE_AUTO_APPLY set — promoting {enhance_id} without diff review")
        promote_enhancement(enhance_id)


def promote_enhancement(enhance_id: str) -> dict:
    """Diff gate approval: back up the live workspace, copy the staged changes
    over it, then drop the staging copy. Returns a small status dict.

    Runs synchronously (a fast file copy); the caller/route guards single-flight.
    """
    enh = state_store.get_enhancement(enhance_id)
    if not enh:
        return {"ok": False, "error": "not_found"}

    staging_path = enh.get("staging_path") or ""
    if not staging_path or not Path(staging_path).exists():
        state_store.update_enhancement_status(enhance_id, "failed", "Staged changes are gone; cannot promote.")
        return {"ok": False, "error": "staging_missing"}

    live_root = resolve_workspace_dir(enh.get("project_id"))
    try:
        # Backup the live workspace first so the promote itself can be rolled back.
        try:
            bp = backup_workspace(live_root, label=f"{enhance_id}_prepromote")
            state_store.update_enhancement_status(enhance_id, "running", backup_path=bp)
            _log(None, "orchestrator", f"Pre-promote backup at {bp}")
        except Exception as bk_exc:
            _log(None, "orchestrator", f"Pre-promote backup warning (non-fatal): {bk_exc}", level="error")

        promote_workspace(staging_path, live_root)
        discard_staging(staging_path)
        state_store.set_enhancement_staging(enhance_id, "")
        state_store.update_enhancement_status(enhance_id, "success", "Staged changes promoted to workspace.")
        _log(None, "orchestrator", f"Enhancement {enhance_id} promoted into live workspace")
        return {"ok": True, "status": "success"}
    except Exception as exc:
        state_store.update_enhancement_status(enhance_id, "failed", f"Promote failed: {exc}")
        _log(None, "orchestrator", f"Promote of {enhance_id} failed: {exc}", level="error")
        return {"ok": False, "error": str(exc)}


def discard_enhancement(enhance_id: str) -> dict:
    """Diff gate rejection: throw away the staged changes. The live workspace was
    never touched, so nothing needs restoring."""
    enh = state_store.get_enhancement(enhance_id)
    if not enh:
        return {"ok": False, "error": "not_found"}
    staging_path = enh.get("staging_path") or ""
    if staging_path:
        discard_staging(staging_path)
        state_store.set_enhancement_staging(enhance_id, "")
    state_store.update_enhancement_status(enhance_id, "discarded", "Staged changes discarded; workspace unchanged.")
    _log(None, "orchestrator", f"Enhancement {enhance_id} staged changes discarded")
    return {"ok": True, "status": "discarded"}
