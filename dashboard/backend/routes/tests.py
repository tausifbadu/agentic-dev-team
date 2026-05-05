"""Test results API routes."""

from pathlib import Path

from fastapi import APIRouter

import sys
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import state_store

router = APIRouter(tags=["tests"])


@router.get("/tests/results")
def get_test_results(storypack_id: str | None = None, limit: int = 50):
    return state_store.get_test_results(storypack_id=storypack_id, limit=limit)
