#!/usr/bin/env python3
"""Agentic Dev Team v1. Requirement -> PM -> Review -> Frontend/Backend/Test Agents."""

import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from schemas import Requirement, StoryPack
import state_store
from state_store import requirement_path, save, storypack_path
from agents.pm_agent import create_stories
from agents.backend_agent import implement_backend
from agents.frontend_agent import implement_frontend
from agents.test_agent import implement_tests
from workspace_paths import resolve_workspace_dir

PROMPT_DIR = Path(__file__).parent / "prompt"


def _use_legacy_cli() -> bool:
    """Match dashboard: USE_LEGACY_PIPELINE=1 forces legacy; CLI also accepts --legacy."""
    if os.getenv("USE_LEGACY_PIPELINE", "0").strip().lower() in ("1", "true", "yes"):
        return True
    return "--legacy" in sys.argv


def _strip_legacy_flag_from_argv() -> None:
    if "--legacy" in sys.argv:
        sys.argv = [a for a in sys.argv if a != "--legacy"]


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


def _run_frontend(stories, pack: StoryPack):
    if not stories:
        print("No frontend stories.")
        return True

    print("\nImplementing frontend stories...")
    for story in stories:
        ok, msg = implement_frontend(story, pack.requirement_text, pack.stories)
        if ok:
            print(f"  {story.id} {story.title}: {msg}")
        else:
            print(f"  {story.id} {story.title}: FAILED")
            print(msg)
            return False
    return True


def _run_backend(stories, pack: StoryPack):
    if not stories:
        print("No backend stories.")
        return True

    print("\nImplementing backend stories...")
    for story in stories:
        ok, msg = implement_backend(story, pack.requirement_text, pack.stories)
        if ok:
            print(f"  {story.id} {story.title}: {msg}")
        else:
            print(f"  {story.id} {story.title}: FAILED")
            print(msg)
            return False
    return True


def _run_tests(pack: StoryPack):
    print("\nGenerating and running tests...")
    ok, msg = implement_tests(pack.stories, pack.requirement_text)
    if ok:
        print(f"  Tests: {msg}")
    else:
        print("  Tests: FAILED")
        print(msg)
    return ok


def _resolve_requirement_text() -> str:
    """Parse CLI args and return the requirement text.

    Supported modes:
      python main.py "inline requirement text"
      python main.py --file prompt/webhook.txt
      python main.py --file webhook          (auto-resolves from prompt/ dir)
      python main.py --list                  (list available prompt files)
    """
    if len(sys.argv) < 2:
        print("Usage:")
        print('  python main.py "Your requirement here"')
        print("  python main.py --file prompt/webhook.txt")
        print("  python main.py --file webhook")
        print("  python main.py --list")
        print("\nRuntime (after story approval):")
        print("  Default: Supervisor + ReAct + tools (same as dashboard).")
        print("  Legacy:  USE_LEGACY_PIPELINE=1  or  python main.py --legacy ...")
        sys.exit(1)

    if sys.argv[1] == "--list":
        files = sorted(PROMPT_DIR.glob("*.txt")) if PROMPT_DIR.exists() else []
        if not files:
            print("No prompt files found in prompt/")
        else:
            print("Available prompt files:")
            for f in files:
                first_line = f.read_text(encoding="utf-8").split("\n", 1)[0].strip()
                print(f"  {f.stem:<20} {first_line[:60]}")
        sys.exit(0)

    if sys.argv[1] == "--file":
        if len(sys.argv) < 3:
            print("Error: --file requires a path argument.")
            sys.exit(1)
        file_arg = sys.argv[2]
        path = Path(file_arg)
        if not path.exists():
            path = PROMPT_DIR / f"{file_arg}.txt"
        if not path.exists():
            path = PROMPT_DIR / file_arg
        if not path.exists():
            print(f"Error: file not found: {file_arg}")
            print("Run 'python main.py --list' to see available prompt files.")
            sys.exit(1)
        return path.read_text(encoding="utf-8").strip()

    return " ".join(sys.argv[1:])


def _run_via_supervisor(pack: StoryPack) -> bool:
    """Agentic runtime: same Supervisor path as the dashboard (default)."""
    from agents.supervisor import Supervisor, SupervisorConfig

    workspace_dir = resolve_workspace_dir("default")

    fast_track = _env_truthy("AGENTIC_FAST_TRACK")

    def _on_progress(agent_id: str, level: str, message: str, detail=None):
        print(f"[{agent_id}] {message}", flush=True)
        if detail:
            d = detail.strip()
            if len(d) > 800:
                d = d[:800] + "…"
            print(d, flush=True)

    print(
        "\nUsing agentic runtime (Supervisor + ReAct + tools). "
        f"Workspace: {workspace_dir}\n",
        flush=True,
    )

    supervisor = Supervisor(
        storypack_id=pack.id,
        requirement_text=pack.requirement_text,
        stories=list(pack.stories),
        workspace_dir=workspace_dir,
        config=SupervisorConfig(
            run_tests=not fast_track,
            run_smoke=(not fast_track)
            and os.getenv("AGENTIC_RUN_SMOKE", "1").strip().lower() not in ("0", "false", "no", "off"),
        ),
        on_progress=_on_progress,
        all_stories=list(pack.stories),
    )
    result = supervisor.run()

    print(result.summary, flush=True)
    return result.success


def _run_legacy_pipeline(pack: StoryPack) -> bool:
    """Original plan-and-patch orchestration (USE_LEGACY_PIPELINE or --legacy)."""
    print("\nUsing LEGACY runtime (plan → JSON patch per story).\n", flush=True)

    state_store.update_storypack_status(pack.id, "in_progress")

    backend_stories = [s for s in pack.stories if s.ownership == "backend"]
    frontend_stories = [s for s in pack.stories if s.ownership == "frontend"]

    ok = True
    if not _run_backend(backend_stories, pack):
        ok = False
    elif not _run_frontend(frontend_stories, pack):
        ok = False
    elif not _run_tests(pack):
        ok = False

    state_store.update_storypack_status(pack.id, "completed" if ok else "failed")
    pack.status = "completed" if ok else "failed"
    save(storypack_path(pack.id), pack.model_dump())
    return ok


def main():
    use_legacy = _use_legacy_cli()
    _strip_legacy_flag_from_argv()

    text = _resolve_requirement_text()
    req_id = f"req_{uuid.uuid4().hex[:8]}"

    req = Requirement(id=req_id, text=text)
    save(requirement_path(req_id), req.model_dump())

    print("Creating stories...")
    pack = create_stories(req)
    save(storypack_path(pack.id), pack.model_dump())

    print("\n" + "=" * 60 + "\nSTORIES FOR REVIEW\n" + "=" * 60)
    for s in pack.stories:
        deps = f" deps={s.dependencies}" if s.dependencies else ""
        print(f"\n[{s.id}] {s.title} ({s.ownership}){deps}")
        print(f"  {s.description}")
        for ac in s.acceptance_criteria:
            print(f"  - {ac}")
        for note in s.implementation_notes:
            print(f"  note: {note}")
        for test_focus in s.test_focus:
            print(f"  test: {test_focus}")
    print("\n" + "=" * 60)
    print("Type 'approve' or 'reject': ", end="", flush=True)
    choice = input().strip().lower()

    if choice != "approve":
        print("Rejected. Exiting.")
        pack.status = "rejected"
        save(storypack_path(pack.id), pack.model_dump())
        sys.exit(0)

    pack.status = "approved"
    save(storypack_path(pack.id), pack.model_dump())

    if use_legacy:
        if not _run_legacy_pipeline(pack):
            sys.exit(1)
        print("\nDone.")
        return

    state_store.update_storypack_status(pack.id, "in_progress")
    success = _run_via_supervisor(pack)
    state_store.update_storypack_status(pack.id, "completed" if success else "failed")
    pack.status = "completed" if success else "failed"
    save(storypack_path(pack.id), pack.model_dump())

    if not success:
        sys.exit(1)
    print("\nDone.")


if __name__ == "__main__":
    main()
