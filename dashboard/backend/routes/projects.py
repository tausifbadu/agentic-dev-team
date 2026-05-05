"""Per-project workspace roots under workspace/projects/<slug>/."""

import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from workspace_paths import ensure_project_layout, list_project_slugs, normalize_project_id, resolve_workspace_dir

router = APIRouter(tags=["projects"])


class ProjectCreate(BaseModel):
    slug: str


@router.get("/projects")
def list_projects():
    slugs = list_project_slugs()
    return {
        "projects": [
            {"id": s, "path": str(resolve_workspace_dir(s))}
            for s in slugs
        ],
    }


@router.post("/projects")
def create_project(body: ProjectCreate):
    slug = normalize_project_id(body.slug)
    if slug == "default":
        raise HTTPException(status_code=400, detail="Cannot create a project with reserved id 'default'.")
    root = ensure_project_layout(slug)
    return {"id": slug, "path": str(root)}
