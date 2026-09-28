"""Structured coding input/output; suggested tests are descriptions, not executable actions."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.schemas.context import IssueContext
from app.schemas.planning import PlanTaskProposal

CodingText = Annotated[str, StringConstraints(min_length=1, max_length=2000)]


class DependencyOutcome(BaseModel):
    task_key: str
    status: str
    summary: str | None


class CodingInput(BaseModel):
    task: PlanTaskProposal
    context: IssueContext
    dependency_outcomes: list[DependencyOutcome]
    workspace_status: str


class CodeChangeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1, max_length=10000)
    files_changed: tuple[str, ...] = Field(min_length=1, max_length=50)
    unified_diff: str = Field(min_length=1, max_length=262144)
    assumptions: tuple[CodingText, ...] = Field(max_length=50)
    tests_to_run: tuple[CodingText, ...] = Field(max_length=50)
