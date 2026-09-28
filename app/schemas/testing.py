"""Trusted test selection inputs and structured sandbox results."""

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.context import ContextRepository
from app.schemas.planning import PlanTaskProposal


class RepositoryTestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    required_commands: tuple[str, ...] = Field(min_length=1, max_length=8)


class TestInput(BaseModel):
    repository: ContextRepository
    task: PlanTaskProposal
    coding_diff: str = Field(max_length=1048576)
    suggested_tests: tuple[str, ...]
    configuration: RepositoryTestConfig


class TestResult(BaseModel):
    command: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    duration: float
    passed: bool
    timeout: bool
    output_truncated: bool


class TestReport(BaseModel):
    results: list[TestResult]
    passed: bool
