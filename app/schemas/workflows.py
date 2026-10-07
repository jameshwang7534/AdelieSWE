"""Public workflow requests/status; never expose stored source context or provider inputs."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.repositories import RegisterRepository


class WorkflowRequest(RegisterRepository):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    issue_number: int = Field(gt=0)
    publish_pull_request: bool = Field(default=False, strict=True)


class WorkflowStatus(BaseModel):
    id: UUID
    stage: str
    status: str
    correlation_id: UUID | None = None
    generation: int = 0
    retry_at: datetime | None = None
    attempts: int
    error_code: str | None
    repository_id: UUID | None = None
    issue_id: UUID | None = None
    plan_id: UUID | None = None
    execution_id: UUID | None = None
    pull_request_url: str | None = None
    history: list[dict[str, object]]
    updated_at: datetime
