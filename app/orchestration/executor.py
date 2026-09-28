"""Explicit executor seam. No production coding executor is configured in this step."""

from typing import Protocol
from uuid import UUID

from app.schemas.executions import TaskExecutionResponse
from app.services.executions import ExecutionService


class TaskExecutor(Protocol):
    def execute(self, task: TaskExecutionResponse) -> None: ...


class ExecutorFailure(Exception):
    """An executor reported task failure."""


class Orchestrator:
    def __init__(self, service: ExecutionService, executor: TaskExecutor) -> None:
        self.service, self.executor = service, executor

    def dispatch_ready(self, run_id: UUID) -> int:
        """One wave; independent workers can claim distinct queued tasks concurrently."""
        ready = self.service.reconcile(run_id)
        claimed = 0
        for task in ready.tasks:
            if task.status != "queued" or not self.service.transition(run_id, task.id, "running"):
                continue
            claimed += 1
            # Never keep a database transaction open across executor calls.
            try:
                self.executor.execute(task)
            except ExecutorFailure:
                self.service.transition(run_id, task.id, "failed", "executor_failed")
            except Exception:
                self.service.transition(run_id, task.id, "failed", "executor_unexpected_failure")
                raise
            else:
                self.service.transition(run_id, task.id, "completed")
        return claimed
