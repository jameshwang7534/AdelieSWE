"""Structured model review and independent orchestration approval decision."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.context import IssueContext
from app.schemas.executions import TaskExecutionResponse
from app.schemas.planning import ImplementationPlanProposal
from app.schemas.testing import TestReport


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    severity: Literal["info", "warning", "blocking"]
    file_path: str | None = Field(default=None, max_length=1024)
    line: int | None = Field(default=None, gt=0)
    reference: str | None = Field(default=None, max_length=1000)
    description: str = Field(min_length=1, max_length=4000)
    recommendation: str = Field(min_length=1, max_length=4000)

    @field_validator("file_path")
    @classmethod
    def relative_reference(cls, value: str | None) -> str | None:
        if value is not None and (
            not value
            or "\\" in value
            or ":" in value
            or value.startswith("/")
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("Finding paths must be repository-relative")
        return value


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    summary: str = Field(min_length=1, max_length=10000)
    approved: bool = Field(strict=True)
    findings: tuple[ReviewFinding, ...] = Field(max_length=100)


class ReviewTestEvidence(BaseModel):
    task_key: str
    agent_run_id: UUID
    report: TestReport


class ReviewInput(BaseModel):
    context: IssueContext
    plan: ImplementationPlanProposal
    task_outcomes: list[TaskExecutionResponse]
    full_diff: str
    test_results: list[ReviewTestEvidence]


class ReviewDecision(BaseModel):
    review: ReviewResult | None
    mechanical_errors: tuple[str, ...]
    approved: bool
    diff_hash: str | None
