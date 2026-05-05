"""Workspace file browser API routes."""

from pathlib import Path

from fastapi import APIRouter, HTTPException

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
WORKSPACE = PROJECT_ROOT / "workspace"

router = APIRouter(tags=["workspace"])

IGNORED = {"node_modules", "__pycache__", ".pytest_cache", "dist", "build", ".git", ".venv"}


def _tree(root: Path, prefix: str = "") -> list[dict]:
    """Build a file tree from a directory."""
    if not root.exists():
        return []
    entries = []
    for item in sorted(root.iterdir()):
        if item.name in IGNORED or item.name.startswith("."):
            continue
        rel = str(item.relative_to(WORKSPACE))
        if item.is_dir():
            entries.append({
                "name": item.name,
                "path": rel,
                "type": "directory",
                "children": _tree(item, rel),
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
def list_workspace_files():
    return _tree(WORKSPACE)


@router.get("/workspace/file")
def read_workspace_file(path: str):
    full = (WORKSPACE / path).resolve()
    if not str(full).startswith(str(WORKSPACE.resolve())):
        raise HTTPException(status_code=403, detail="Path traversal not allowed")
    if not full.exists() or not full.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    try:
        content = full.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Binary file, cannot display")

    return {"path": path, "content": content, "size": full.stat().st_size}
