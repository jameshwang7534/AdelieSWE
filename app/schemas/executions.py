"""Run snapshots and persisted orchestration state returned by the API."""

from collections import Counter
from datetime import UTC, datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, computed_field, model_validator

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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def elapsed_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        return max(0, ((self.completed_at or datetime.now(UTC)) - self.started_at).total_seconds())

    @computed_field  # type: ignore[prop-decorator]
    @property
    def task_counts(self) -> dict[str, int]:
        return dict(Counter(task.status for task in self.tasks))
