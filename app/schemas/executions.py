"""Run snapshots and persisted orchestration state returned by the API."""

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from app.schemas.planning import ImplementationPlanProposal


class ExecutionSnapshot(BaseModel):
    proposal: ImplementationPlanProposal
    task_ids: dict[str, UUID]

    @model_validator(mode="after")
    def validate_ids(self) -> Self:
        if set(self.task_ids) != {task.task_key for task in self.proposal.tasks}:
            raise ValueError("Snapshot task keys do not match")
        if len(set(self.task_ids.values())) != len(self.task_ids):
            raise ValueError("Snapshot task IDs must be unique")
        return self


class TaskExecutionResponse(BaseModel):
    id: UUID
    plan_task_id: UUID
    task_key: str
    dependencies: list[str]
    status: str
    attempt: int
    output_summary: str | None


class ExecutionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    plan_id: UUID
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    tasks: list[TaskExecutionResponse]
