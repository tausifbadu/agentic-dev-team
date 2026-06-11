"""Background agent execution for the dashboard API."""

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
from agents.reasoning import backup_workspace, extract_api_contract, save_json

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

def run_agents_background(
    pack_id: str,
    *,
    fast_track: bool | None = None,
    story_ids: list[str] | None = None,
) -> None:
    """Run all agents for an approved storypack.

    Dispatches to the new agentic supervisor (default) unless USE_LEGACY_PIPELINE=1.

    fast_track: when True, skip test phase and smoke (supervisor only). If None,
    reads AGENTIC_FAST_TRACK from the environment.

    story_ids: when set and non-empty, only these stories (plus transitive
    prerequisites per ``Story.dependencies``) are executed. Agents still receive
    the full pack as context via ``Supervisor(all_stories=...)``.
    """
    ft = fast_track if fast_track is not None else _env_truthy("AGENTIC_FAST_TRACK")
    if USE_AGENTIC_SUPERVISOR:
        return _run_agents_via_supervisor(pack_id, fast_track=ft, story_ids=story_ids)
    return _run_agents_legacy(pack_id, story_ids=story_ids)


def _run_agents_via_supervisor(
    pack_id: str,
    *,
    fast_track: bool = False,
    story_ids: list[str] | None = None,
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
    try:
        stories, auto_included = expand_story_selection(full_stories, story_ids)
    except ValueError as exc:
        _log(None, "orchestrator", f"Invalid story selection: {exc}", level="error")
        return
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
                run_tests=not fast_track,
                run_smoke=(not fast_track)
                and os.getenv("AGENTIC_RUN_SMOKE", "1").strip().lower() not in ("0", "false", "no", "off"),
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


def _run_agents_legacy(pack_id: str, *, story_ids: list[str] | None = None) -> None:
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
    try:
        stories, auto_included = expand_story_selection(full_stories, story_ids)
    except ValueError as exc:
        _log(None, "orchestrator", f"Invalid story selection: {exc}", level="error")
        return
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


def _run_single_enhance_agent(
    story: Story,
    agent_type: str,
    enhancement_context: str,
    description: str,
    all_stories: list,
    workspace_root: Path,
) -> tuple[bool, str]:
    """Implement one enhancement story with heal loop. Returns (ok, message)."""
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


def run_enhancement_background(enhance_id: str) -> None:
    """Run an enhancement: PM creates story/stories, agent(s) implement, heal loop on failure."""
    global enhance_state, _current_pack_id, _current_run_id

    enh = state_store.get_enhancement(enhance_id)
    if not enh:
        return

    agent_type = enh["agent_type"]
    description = enh["description"]
    context = enh.get("context", "")

    _current_pack_id = f"enhance-{enhance_id}"
    _current_run_id = f"enhance-run-{enhance_id}"

    enhance_state = {
        "running": True,
        "enhance_id": enhance_id,
        "agent_type": agent_type,
        "phase": "pm_story",
    }

    state_store.update_enhancement_status(enhance_id, "running")
    _log(None, "orchestrator", f"Enhancement {enhance_id} started — PM generating story for {agent_type}")

    workspace_root = resolve_workspace_dir(enh.get("project_id"))

    try:
        enhancement_context = _build_enhancement_context(description, context, workspace_root)

        _log(None, "orchestrator", f"Creating workspace backup before enhancement {enhance_id}")
        try:
            bp = backup_workspace(workspace_root, label=enhance_id)
            state_store.update_enhancement_status(enhance_id, "running", backup_path=bp)
            _log(None, "orchestrator", f"Workspace backed up to {bp}")
        except Exception as bk_exc:
            _log(None, "orchestrator", f"Backup warning (non-fatal): {bk_exc}", level="error")

        if agent_type == "both":
            stories_data = create_enhancement_stories_both(description, context)
            stories = [Story(**sd) for sd in stories_data]
            state_store.update_enhancement_status(
                enhance_id, "running",
                story_json=json.dumps(stories_data, default=str),
            )
            for s in stories:
                _log(None, "pm", f"Enhancement story created: {s.title} ({s.ownership})",
                     detail=json.dumps(s.__dict__ if hasattr(s, '__dict__') else {}, indent=2, default=str))

            be_stories = [s for s in stories if s.ownership == "backend"]
            fe_stories = [s for s in stories if s.ownership == "frontend"]

            all_ok = True
            results: list[str] = []

            for story in be_stories:
                enhance_state["phase"] = "backend"
                enhance_state["agent_type"] = "backend"
                _log(story.id, "backend", f"Starting backend enhancement: {story.title}")
                ok, msg = _run_single_enhance_agent(
                    story, "backend", enhancement_context, description, stories, workspace_root,
                )
                results.append(f"backend ({story.title}): {'OK' if ok else 'FAILED'}")
                if not ok:
                    all_ok = False
                    break

            if all_ok:
                _publish_contract(workspace_root)

            if all_ok:
                for story in fe_stories:
                    enhance_state["phase"] = "frontend"
                    enhance_state["agent_type"] = "frontend"
                    _log(story.id, "frontend", f"Starting frontend enhancement: {story.title}")
                    ok, msg = _run_single_enhance_agent(
                        story, "frontend", enhancement_context, description, stories, workspace_root,
                    )
                    results.append(f"frontend ({story.title}): {'OK' if ok else 'FAILED'}")
                    if not ok:
                        all_ok = False
                        break

            summary = " | ".join(results)
            if all_ok:
                state_store.update_enhancement_status(enhance_id, "success", summary)
            else:
                state_store.update_enhancement_status(enhance_id, "failed", summary)

        else:
            story_data = create_enhancement_story(agent_type, description, context)
            story = Story(**story_data)
            state_store.update_enhancement_status(
                enhance_id, "running",
                story_json=json.dumps(story_data, default=str),
            )
            _log(None, "pm", f"Enhancement story created: {story.title}",
                 detail=json.dumps(story_data, indent=2))

            enhance_state["phase"] = "implementing"
            ok, msg = _run_single_enhance_agent(
                story, agent_type, enhancement_context, description, [story], workspace_root,
            )

            if ok:
                state_store.update_enhancement_status(enhance_id, "success", msg)
            else:
                state_store.update_enhancement_status(enhance_id, "failed", msg)

    except Exception as exc:
        _log(None, "orchestrator", f"Enhancement {enhance_id} crashed: {exc}", level="error")
        state_store.update_enhancement_status(enhance_id, "failed", str(exc))
    finally:
        enhance_state = {
            "running": False,
            "enhance_id": None,
            "agent_type": None,
            "phase": "idle",
        }
