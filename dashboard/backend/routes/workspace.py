"""Workspace file browser API routes."""

import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from workspace_paths import resolve_workspace_dir

router = APIRouter(tags=["workspace"])

IGNORED = {"node_modules", "__pycache__", ".pytest_cache", "dist", "build", ".git", ".venv"}


def _tree(root: Path, base: Path) -> list[dict]:
    """Build a file tree from a directory; paths are relative to ``base``."""
    if not root.exists():
        return []
    entries = []
    for item in sorted(root.iterdir()):
        if item.name in IGNORED or item.name.startswith("."):
            continue
        rel = str(item.relative_to(base))
        if item.is_dir():
            entries.append({
                "name": item.name,
                "path": rel,
                "type": "directory",
                "children": _tree(item, base),
            })
        else:
            entries.append({
                "name": item.name,
                "path": rel,
                "type": "file",
                "size": item.stat().st_size,
            })
    return entries


@router.get("/workspace/files")
def list_workspace_files(project_id: str | None = None):
    base = resolve_workspace_dir(project_id)
    return _tree(base, base)


@router.get("/workspace/file")
def read_workspace_file(path: str, project_id: str | None = None):
    ws = resolve_workspace_dir(project_id)
    full = (ws / path).resolve()
    if not str(full).startswith(str(ws.resolve())):
        raise HTTPException(status_code=403, detail="Path traversal not allowed")
    if not full.exists() or not full.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    try:
        content = full.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Binary file, cannot display")

    return {"path": path, "content": content, "size": full.stat().st_size}
