"""Persisted plan responses also support legacy records with optional metadata."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class PlanTaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    task_key: str
    title: str
    description: str | None
    rationale: str | None
    target_files: list[str]
    dependencies: list[str]
    acceptance_criteria: list[str]
    suggested_tests: list[str]
    status: str
    sequence: int


class PlanResponse(BaseModel):
    id: UUID
    issue_id: UUID
    summary: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    tasks: list[PlanTaskResponse]
