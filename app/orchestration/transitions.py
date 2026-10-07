"""Validated state changes. Callers must hold the owning workflow/execution row lock."""

from collections.abc import Mapping, Set
from datetime import UTC, datetime

from sqlalchemy.orm import object_session

from app.models import AgentRun, ExecutionRun, TaskExecution, WorkflowRun
from app.orchestration.state import InvalidTransition


def record_transition(
    record: AgentRun | ExecutionRun | TaskExecution | WorkflowRun, target: str
) -> None:
    if record.status == target:
        return
    session = object_session(record)
    if session is None:
        return
    fields: dict[str, object] = {"from_state": record.status, "to_state": target}
    key = {
        AgentRun: "agent_run_id",
        ExecutionRun: "execution_id",
        TaskExecution: "task_execution_id",
        WorkflowRun: "workflow_id",
    }[type(record)]
    fields[key] = record.id
    if isinstance(record, AgentRun) and record.task_execution_id is not None:
        task = session.get(TaskExecution, record.task_execution_id)
        if task is not None:
            fields["plan_task_id"] = task.plan_task_id
    for name in ("execution_run_id", "plan_task_id", "plan_id", "task_execution_id"):
        if hasattr(record, name):
            fields["execution_id" if name == "execution_run_id" else name] = getattr(record, name)
    if (
        isinstance(record, ExecutionRun)
        and record.started_at
        and target in {"completed", "failed", "cancelled"}
    ):
        fields["duration_ms"] = max(
            0, (datetime.now(UTC) - record.started_at).total_seconds() * 1000
        )
    session.info.setdefault("log_transitions", []).append(fields)


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
    record_transition(record, target)
    record.status = target


def execution_state(record: ExecutionRun, target: str) -> None:
    _check(record.status, target, RUN_EDGES)
    record_transition(record, target)
    record.status = target


def workflow_state(record: WorkflowRun, target: str) -> None:
    _check(record.status, target, WORKFLOW_EDGES)
    record_transition(record, target)
    record.status = target


def agent_state(record: AgentRun, target: str) -> None:
    # Publication journals reconcile a successful remote operation after local failure.
    edges = {"running": {"completed", "failed"}}
    if record.agent_type == "pull_request":
        edges["failed"] = {"completed"}
    _check(record.status, target, edges)
    record_transition(record, target)
    record.status = target
