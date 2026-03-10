"""Data schemas for the Agentic Dev Team. JSON files in ./state/. No ORM."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Requirement(BaseModel):
    id: str
    text: str
    submitted_at: datetime = Field(default_factory=datetime.utcnow)


class Story(BaseModel):
    id: str
    title: str
    description: str
    acceptance_criteria: list[str]
    ownership: Literal["frontend", "backend"]
    status: Literal["pending_review", "approved", "rejected", "in_progress", "done"] = "pending_review"


class StoryPack(BaseModel):
    id: str
    requirement_id: str
    requirement_text: str
    stories: list[Story]
    status: Literal["pending_review", "approved", "rejected"] = "pending_review"
    created_at: datetime = Field(default_factory=datetime.utcnow)
