"""Data schemas for the Agentic Dev Team."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Requirement(BaseModel):
    id: str
    text: str
    submitted_at: datetime = Field(default_factory=_utcnow)


class Story(BaseModel):
    id: str
    title: str
    description: str
    acceptance_criteria: list[str]
    implementation_notes: list[str] = Field(default_factory=list)
    test_focus: list[str] = Field(default_factory=list)
    ownership: Literal["frontend", "backend", "testing"]
    dependencies: list[str] = Field(default_factory=list)
    status: Literal["pending_review", "approved", "rejected", "in_progress", "done"] = "pending_review"


class StoryPack(BaseModel):
    id: str
    requirement_id: str
    requirement_text: str
    stories: list[Story]
    status: Literal["pending_review", "approved", "in_progress", "completed", "failed", "rejected"] = "pending_review"
    created_at: datetime = Field(default_factory=_utcnow)
