"""Standalone test runner — run the Test Agent against an existing POC workspace,
independent of a full build.

The build pipeline runs the test agent as Supervisor Phase 3; this lets the dashboard
(or CLI) re-run the whole suite on demand against a workspace that already exists. Both
paths use the SAME TestAgent and therefore the SAME ledger recorder — the only
difference is the recorded `trigger` ("manual" here vs "build" in the pipeline).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Callable, Optional

from schemas import Story


def _load_project_context(project_id: str):
    """Best-effort: latest storypack for this project -> (pack_id, requirement, stories).
    Gives the test agent acceptance criteria / naming context. Empty is fine."""
    pack_id, requirement, stories = "", "", []
    try:
        import state_store
        packs = [p for p in state_store.list_storypacks(limit=200)
                 if (p.get("project_id") or "default") == project_id]
        if packs:
            latest = packs[0]  # list_storypacks is newest-first
            pack_id = latest.get("id", "")
            full = state_store.get_storypack(pack_id) or {}
            requirement = full.get("requirement_text", "") or ""
            for d in (full.get("stories") or []):
                try:
                    stories.append(Story(**{k: d[k] for k in d if k in Story.model_fields}))
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass
    return pack_id, requirement, stories


def run_tests_for_project(
    project_id: str = "default",
    *,
    run_id: Optional[str] = None,
    trigger: str = "manual",
    on_progress: Optional[Callable[[str, str, Optional[str]], None]] = None,
) -> dict:
    """Run the Test Agent against `project_id`'s workspace and record the run.

    Returns {success, summary, run_id, project_id, workspace_dir}. Reuses TestAgent,
    so the harness scaffold, seed data, directives, run_pytest + run_e2e validation,
    and the ledger recording all happen exactly as in the build pipeline."""
    from agents.agentic.base import RunContext, Budget
    from agents.agentic.test import TestAgent
    from agents.bus import MessageBus
    from agents.supervisor import _make_synthetic_test_story
    from workspace_paths import normalize_project_id, resolve_workspace_dir

    pid = normalize_project_id(project_id)
    workspace_dir = resolve_workspace_dir(pid)
    run_id = run_id or f"testrun_{uuid.uuid4().hex[:8]}"
    pack_id, requirement, stories = _load_project_context(pid)

    bus = MessageBus(storypack_id=pack_id or f"tests-{pid}", run_id=run_id)
    ctx = RunContext(
        storypack_id=pack_id or f"tests-{pid}",
        run_id=run_id,
        workspace_dir=Path(workspace_dir),
        bus=bus,
        budget=Budget.from_env(),
        requirement_text=requirement,
        all_stories=stories,
    )

    agent = TestAgent(ctx, on_progress=on_progress or (lambda level, msg, detail=None: None))
    agent.trigger = trigger
    agent.register_with_bus()
    try:
        result = agent.run(_make_synthetic_test_story())
    finally:
        try:
            agent.unregister()
        except Exception:  # noqa: BLE001
            pass
        try:
            bus.close()
        except Exception:  # noqa: BLE001
            pass

    return {
        "success": bool(result.success),
        "summary": result.summary or "",
        "run_id": run_id,
        "project_id": pid,
        "workspace_dir": str(workspace_dir),
    }
