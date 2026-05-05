"""Resolve per-project workspace directories under the repo.

- ``default`` → ``<repo>/workspace`` (legacy single-tree layout).
- Any other project id → ``<repo>/workspace/projects/<id>/`` with the same
  internal shape (``backend/``, ``frontend/``, …).
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
LEGACY_WORKSPACE_ROOT = PROJECT_ROOT / "workspace"
PROJECTS_PARENT = LEGACY_WORKSPACE_ROOT / "projects"

_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


def normalize_project_id(raw: str | None) -> str:
    if raw is None:
        return "default"
    s = str(raw).strip().lower()
    if not s:
        return "default"
    s = _SLUG_RE.sub("-", s).strip("-")
    if not s:
        return "default"
    return s[:80]


def resolve_workspace_dir(project_id: str | None) -> Path:
    """Root directory containing backend/, frontend/, contracts/, etc."""
    pid = normalize_project_id(project_id)
    if pid == "default":
        return LEGACY_WORKSPACE_ROOT
    return PROJECTS_PARENT / pid


def ensure_project_layout(project_id: str) -> Path:
    """Create ``workspace/projects/<id>/`` with standard subdirs if missing."""
    root = resolve_workspace_dir(project_id)
    pid = normalize_project_id(project_id)
    if pid == "default":
        return root
    root.mkdir(parents=True, exist_ok=True)
    for sub in ("backend", "frontend", "contracts", "tests"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def list_project_slugs() -> list[str]:
    slugs = ["default"]
    if PROJECTS_PARENT.exists():
        for p in sorted(PROJECTS_PARENT.iterdir()):
            if p.is_dir() and not p.name.startswith(".") and p.name not in slugs:
                slugs.append(p.name)
    return slugs
