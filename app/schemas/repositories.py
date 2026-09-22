"""Public repository and imported issue contracts."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RegisterRepository(BaseModel):
    github_owner: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]*$", max_length=255)
    github_name: str = Field(pattern=r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*$", max_length=255)


class RepositoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    github_owner: str
    github_name: str
    clone_url: str
    default_branch: str
    local_status: str
    index_status: str
    created_at: datetime
    updated_at: datetime


class IssueResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    repository_id: UUID
    github_issue_number: int
    title: str
    body: str | None
    state: str
    source_url: str
    created_at: datetime
    updated_at: datetime
