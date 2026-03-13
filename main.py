#!/usr/bin/env python3
"""Agentic Dev Team v0. Requirement -> PM -> Review -> Frontend/Backend Agents."""

import sys
import uuid

from schemas import Requirement
from state_store import requirement_path, save, storypack_path
from agents.pm_agent import create_stories
from agents.backend_agent import implement_backend
from agents.frontend_agent import implement_frontend


def _run_frontend(stories):
    if not stories:
        print("No frontend stories.")
        return True

    print("\nImplementing frontend stories...")
    for story in stories:
        ok, msg = implement_frontend(story)
        if ok:
            print(f"  {story.id} {story.title}: {msg}")
        else:
            print(f"  {story.id} {story.title}: FAILED")
            print(msg)
            return False
    return True


def _run_backend(stories):
    if not stories:
        print("No backend stories.")
        return True

    print("\nImplementing backend stories...")
    for story in stories:
        ok, msg = implement_backend(story)
        if ok:
            print(f"  {story.id} {story.title}: {msg}")
        else:
            print(f"  {story.id} {story.title}: FAILED")
            print(msg)
            return False
    return True


def main():
    if len(sys.argv) < 2:
        print("Usage: python main.py \"Your requirement here\"")
        sys.exit(1)

    text = " ".join(sys.argv[1:])
    req_id = f"req_{uuid.uuid4().hex[:8]}"

    req = Requirement(id=req_id, text=text)
    save(requirement_path(req_id), req.model_dump())

    print("Creating stories...")
    pack = create_stories(req)
    save(storypack_path(pack.id), pack.model_dump())

    print("\n" + "=" * 60 + "\nSTORIES FOR REVIEW\n" + "=" * 60)
    for s in pack.stories:
        print(f"\n[{s.id}] {s.title} ({s.ownership})")
        print(f"  {s.description}")
        for ac in s.acceptance_criteria:
            print(f"  - {ac}")
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

    frontend_stories = [s for s in pack.stories if s.ownership == "frontend"]
    backend_stories = [s for s in pack.stories if s.ownership == "backend"]

    if not frontend_stories and not backend_stories:
        print("No stories to implement. Done.")
        return

    if not _run_frontend(frontend_stories):
        sys.exit(1)

    if not _run_backend(backend_stories):
        sys.exit(1)

    print("\nDone.")


if __name__ == "__main__":
    main()
