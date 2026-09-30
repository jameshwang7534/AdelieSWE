"""Bounded debugging context and recovery outcome."""

from pydantic import BaseModel

from app.schemas.context import IssueContext
from app.schemas.planning import PlanTaskProposal
from app.schemas.testing import TestReport, TestResult


class DebugAttempt(BaseModel):
    number: int
    status: str
    summary: str
    error: str | None


class DebugInput(BaseModel):
    context: IssueContext
    task: PlanTaskProposal
    current_diff: str
    failing_tests: list[TestResult]
    previous_attempts: list[DebugAttempt]


class RecoveryReport(BaseModel):
    passed: bool = False
    debug_attempts: int = 0
    tests: list[TestReport] = []
    error: str | None = None
