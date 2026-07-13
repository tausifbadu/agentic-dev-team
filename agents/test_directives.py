"""User-authored test directives (Test Guidance).

Directives are the human test-intent layer on top of what the agent auto-derives
from the contract and stories: business flows, process flows, data flows, edge
cases, negative cases. They live in the canonical, user-editable

    <workspace>/tests/test_directives.json

and are mirrored into the `test_directive` table for the dashboard. Content is
user-owned — the agent never edits or deletes a directive; the recorder only fills
in each directive's `status` and `linked_test_ids` from the tests that reference it
via a `# directive: <id>` annotation.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

KINDS = (
    "business_flow", "process_flow", "data_flow", "integration_flow",
    "edge_case", "negative_case", "ui", "api", "note",
)
PRIORITIES = ("high", "medium", "low")
STATUSES = ("uncovered", "covered", "passed", "failed")

# `# directive: dir_7a3f` / `# directive: dir_1, dir_2`
DIRECTIVE_ANNOT_RE = re.compile(r"#\s*directive:\s*([^\n]+)", re.IGNORECASE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:24] or "directive"


def _directives_path(tests_dir) -> Path:
    return Path(tests_dir) / "test_directives.json"


def normalize_directive(raw: dict, index: int = 0) -> dict:
    """Coerce a user/file directive into the canonical shape, filling safe defaults."""
    d = dict(raw or {})
    did = str(d.get("id") or "").strip() or f"dir_{_slug(d.get('title', ''))}_{index + 1}"
    kind = d.get("kind", "business_flow")
    if kind not in KINDS:
        kind = "business_flow"
    priority = d.get("priority", "medium")
    if priority not in PRIORITIES:
        priority = "medium"
    steps = d.get("steps", [])
    if isinstance(steps, str):
        steps = [s.strip() for s in steps.splitlines() if s.strip()]
    status = d.get("status", "uncovered")
    if status not in STATUSES:
        status = "uncovered"
    return {
        "id": did,
        "kind": kind,
        "title": (d.get("title", "") or "").strip(),
        "steps": list(steps),
        "expected_outcome": (d.get("expected_outcome", "") or "").strip(),
        "priority": priority,
        "status": status,
        "linked_test_ids": list(d.get("linked_test_ids", []) or []),
        "created_by": d.get("created_by", "user"),
        "updated_at": d.get("updated_at", "") or _now(),
    }


def load_directives(tests_dir) -> list[dict]:
    """Read + normalize directives from the canonical file ([] if absent/invalid)."""
    p = _directives_path(tests_dir)
    if not p.exists():
        return []
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = blob.get("directives") if isinstance(blob, dict) else blob
    if not isinstance(items, list):
        return []
    return [normalize_directive(d, i) for i, d in enumerate(items)]


def save_directives(tests_dir, directives: list[dict]) -> None:
    """Write the canonical file (create tests dir if needed)."""
    p = _directives_path(tests_dir)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({
                "_help": ("User-authored test intent. Each directive: {id, kind "
                          f"({'|'.join(KINDS)}), title, steps[], expected_outcome, "
                          "priority (high|medium|low)}. status + linked_test_ids are "
                          "maintained automatically. Annotate a test with "
                          "`# directive: <id>` to link it."),
                "directives": directives,
            }, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def ensure_directives_file(tests_dir) -> bool:
    """Create an empty directives file if missing so the user knows where to add them.
    Returns True if it created one."""
    p = _directives_path(tests_dir)
    if p.exists():
        return False
    save_directives(tests_dir, [])
    return True


def sync_to_db(project_id: str, directives: list[dict]) -> None:
    """Best-effort mirror of the canonical file into the test_directive table."""
    try:
        import state_store
        state_store.replace_test_directives(project_id, directives)
    except Exception:  # noqa: BLE001 — mirror must never break a run
        pass


def parse_directive_refs(text: str) -> list[str]:
    """Extract directive ids referenced by `# directive: <id>[, <id>]` in test source."""
    refs: list[str] = []
    for m in DIRECTIVE_ANNOT_RE.finditer(text or ""):
        refs.extend(x.strip() for x in re.split(r"[,\s]+", m.group(1)) if x.strip())
    return refs
