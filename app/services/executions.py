"""Transactional orchestration. A run-row lock serializes competing schedulers."""

from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import session_scope
from app.models import ExecutionRun, ImplementationPlan, PlanTask, TaskExecution
from app.orchestration.state import TERMINAL, InvalidTransition, advance
from app.schemas.executions import ExecutionResponse, ExecutionSnapshot, TaskExecutionResponse
from app.schemas.planning import ImplementationPlanProposal, PlanTaskProposal
from app.services.repositories import RecordNotFound


class InvalidExecutionPlan(Exception):
    """A legacy, empty, corrupt, or invalid plan cannot be orchestrated."""


class ExecutionService:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def create(self, plan_id: UUID) -> UUID:
        with session_scope(self.sessions) as session:
            plan = session.scalar(
                select(ImplementationPlan).where(ImplementationPlan.id == plan_id).with_for_update()
            )
            if plan is None:
                raise RecordNotFound
            tasks = session.scalars(
                select(PlanTask)
                .where(PlanTask.plan_id == plan_id)
                .order_by(PlanTask.task_key)
                .with_for_update()
            ).all()
            try:
                proposal = ImplementationPlanProposal(
                    summary=plan.summary or "",
                    tasks=tuple(
                        PlanTaskProposal(
                            task_key=task.task_key,
                            title=task.title,
                            description=task.description or "",
                            rationale=task.rationale or "",
                            target_files=tuple(task.target_files),
                            dependencies=tuple(task.dependencies),
                            acceptance_criteria=tuple(task.acceptance_criteria),
                            suggested_tests=tuple(task.suggested_tests),
                        )
                        for task in tasks
                    ),
                )
            except ValidationError:
                raise InvalidExecutionPlan("invalid_execution_plan") from None
            snapshot = ExecutionSnapshot(
                proposal=proposal, task_ids={task.task_key: task.id for task in tasks}
            )
            run = ExecutionRun(
                plan_id=plan_id, status="pending", plan_snapshot=snapshot.model_dump(mode="json")
            )
            session.add(run)
            session.flush()
            session.add_all(
                TaskExecution(
                    execution_run_id=run.id, plan_task_id=task.id, status="pending", attempt=1
                )
                for task in tasks
            )
            identifier = run.id
        return identifier

    def _load(
        self, session: Session, run_id: UUID
    ) -> tuple[ExecutionRun, ExecutionSnapshot, dict[str, TaskExecution]]:
        run = session.scalar(
            select(ExecutionRun).where(ExecutionRun.id == run_id).with_for_update()
        )
        if run is None:
            raise RecordNotFound
        try:
            snapshot = ExecutionSnapshot.model_validate(run.plan_snapshot)
        except ValidationError:
            raise InvalidExecutionPlan("execution_snapshot_unavailable") from None
        records = session.scalars(
            select(TaskExecution).where(TaskExecution.execution_run_id == run_id)
        ).all()
        by_id = {record.plan_task_id: record for record in records}
        if (
            len(records) != len(snapshot.task_ids)
            or set(by_id) != set(snapshot.task_ids.values())
            or any(record.attempt != 1 for record in records)
        ):
            raise InvalidTransition("execution_tasks_mismatch")
        return run, snapshot, {key: by_id[value] for key, value in snapshot.task_ids.items()}

    def _advance(
        self, run: ExecutionRun, snapshot: ExecutionSnapshot, tasks: dict[str, TaskExecution]
    ) -> None:
        states = advance(
            {task.task_key: task.dependencies for task in snapshot.proposal.tasks},
            {key: task.status for key, task in tasks.items()},
        )
        for key, state in states.items():
            tasks[key].status = state
        if run.started_at is None:
            run.started_at = datetime.now(UTC)
        if all(state in TERMINAL for state in states.values()):
            run.status = "completed" if all(s == "completed" for s in states.values()) else "failed"
            if run.completed_at is None:
                run.completed_at = datetime.now(UTC)
        else:
            run.status = "running"

    def reconcile(self, run_id: UUID) -> ExecutionResponse:
        with session_scope(self.sessions) as session:
            run, snapshot, tasks = self._load(session, run_id)
            self._advance(run, snapshot, tasks)
            return self._response(run, snapshot, tasks)

    def transition(
        self, run_id: UUID, task_id: UUID, status: str, output_summary: str | None = None
    ) -> bool:
        if status not in {"running", "completed", "failed"}:
            raise InvalidTransition("unsupported_transition")
        with session_scope(self.sessions) as session:
            run, snapshot, tasks = self._load(session, run_id)
            target = next((task for task in tasks.values() if task.id == task_id), None)
            if target is None:
                raise RecordNotFound
            if target.status == status:
                return False
            # Duplicate worker claims are no-ops, including late delivery after completion.
            if status == "running" and target.status in {
                "running",
                "completed",
                "failed",
                "blocked",
            }:
                return False
            expected = "queued" if status == "running" else "running"
            if target.status != expected:
                raise InvalidTransition("invalid_task_transition")
            target.status = status
            target.output_summary = output_summary[:4000] if output_summary else None
            self._advance(run, snapshot, tasks)
            return True

    def get(self, run_id: UUID) -> ExecutionResponse:
        with session_scope(self.sessions) as session:
            return self._response(*self._load(session, run_id))

    @staticmethod
    def _response(
        run: ExecutionRun, snapshot: ExecutionSnapshot, tasks: dict[str, TaskExecution]
    ) -> ExecutionResponse:
        proposals = {task.task_key: task for task in snapshot.proposal.tasks}
        return ExecutionResponse(
            id=run.id,
            plan_id=run.plan_id,
            status=run.status,
            started_at=run.started_at,
            completed_at=run.completed_at,
            tasks=[
                TaskExecutionResponse(
                    id=tasks[key].id,
                    plan_task_id=tasks[key].plan_task_id,
                    task_key=key,
                    dependencies=list(proposals[key].dependencies),
                    status=tasks[key].status,
                    attempt=tasks[key].attempt,
                    output_summary=tasks[key].output_summary,
                )
                for key in snapshot.proposal.execution_order()
            ],
        )

    def active_ids(self, after: UUID | None = None, limit: int = 100) -> list[UUID]:
        with session_scope(self.sessions) as session:
            query = select(ExecutionRun.id).where(
                ExecutionRun.status.in_(["pending", "running"]),
                ExecutionRun.plan_snapshot.is_not(None),
            )
            if after is not None:
                query = query.where(ExecutionRun.id > after)
            return list(session.scalars(query.order_by(ExecutionRun.id).limit(limit)))
