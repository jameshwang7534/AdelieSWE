"""Validated state changes. Callers must hold the owning workflow/execution row lock."""

from collections.abc import Mapping, Set

from app.models import AgentRun, ExecutionRun, TaskExecution, WorkflowRun
from app.orchestration.state import InvalidTransition

TASK_EDGES = {
    "pending": {"queued", "blocked", "cancelled"},
    "queued": {"running", "failed", "blocked", "cancelled"},
    "running": {"completed", "failed"},
}
RUN_EDGES = {
    "pending": {"running", "failed", "cancelled"},
    "running": {"completed", "failed", "cancelled"},
}
WORKFLOW_EDGES = {
    "pending": {"running", "cancelled"},
    "running": {"pending", "failed", "blocked", "completed"},
    "failed": {"pending", "cancelled"},
    "blocked": {"cancelled"},
}


def _check(current: str, target: str, edges: Mapping[str, Set[str]]) -> None:
    known = set(edges) | {s for values in edges.values() for s in values}
    if target not in known or (target != current and target not in edges.get(current, set())):
        raise InvalidTransition("invalid_state_transition")


def task_state(record: TaskExecution, target: str) -> None:
    _check(record.status, target, TASK_EDGES)
    record.status = target


def execution_state(record: ExecutionRun, target: str) -> None:
    _check(record.status, target, RUN_EDGES)
    record.status = target


def workflow_state(record: WorkflowRun, target: str) -> None:
    _check(record.status, target, WORKFLOW_EDGES)
    record.status = target


def agent_state(record: AgentRun, target: str) -> None:
    # Publication journals reconcile a successful remote operation after local failure.
    edges = {"running": {"completed", "failed"}}
    if record.agent_type == "pull_request":
        edges["failed"] = {"completed"}
    _check(record.status, target, edges)
    record.status = target
