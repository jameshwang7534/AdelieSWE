"""Transactional orchestration. A run-row lock serializes competing schedulers."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import session_scope
from app.models import AgentRun, ExecutionRun, ImplementationPlan, PlanTask, TaskExecution
from app.orchestration.state import TERMINAL, InvalidTransition, advance
from app.orchestration.transitions import agent_state, execution_state, task_state
from app.schemas.executions import ExecutionResponse, ExecutionSnapshot, TaskExecutionResponse
from app.schemas.planning import ImplementationPlanProposal, PlanTaskProposal
from app.services.repositories import RecordNotFound


class InvalidExecutionPlan(Exception):
    """A legacy, empty, corrupt, or invalid plan cannot be orchestrated."""


class ExecutionService:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def create(self, plan_id: UUID, *, identifier: UUID | None = None) -> UUID:
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
                id=identifier,
                plan_id=plan_id,
                status="pending",
                plan_snapshot=snapshot.model_dump(mode="json"),
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
        if run.status in {"failed", "cancelled"}:
            return
        states = advance(
            {task.task_key: task.dependencies for task in snapshot.proposal.tasks},
            {key: task.status for key, task in tasks.items()},
        )
        for key, state in states.items():
            task_state(tasks[key], state)
        if run.started_at is None:
            run.started_at = datetime.now(UTC)
        if all(state in TERMINAL for state in states.values()):
            execution_state(
                run, "completed" if all(s == "completed" for s in states.values()) else "failed"
            )
            if run.completed_at is None:
                run.completed_at = datetime.now(UTC)
        else:
            execution_state(run, "running")

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
            task_state(target, status)
            target.output_summary = output_summary[:4000] if output_summary else None
            self._advance(run, snapshot, tasks)
            return True

    def stop_locked(self, session: Session, run_id: UUID, *, cancelled: bool) -> None:
        """Stop unfinished work atomically; cancellation never races active agent effects."""
        run, _, tasks = self._load(session, run_id)
        agents = session.scalars(
            select(AgentRun).where(
                AgentRun.execution_run_id == run_id, AgentRun.status == "running"
            )
        ).all()
        if cancelled and (agents or any(t.status == "running" for t in tasks.values())):
            raise InvalidTransition("execution_busy")
        if run.status in {"completed", "failed", "cancelled"}:
            # A review or publication agent may still be running after task completion.
            if cancelled or not agents:
                return
        now = datetime.now(UTC)
        reason = "execution_cancelled" if cancelled else "worker_interrupted"
        for agent in agents:
            agent_state(agent, "failed")
            agent.completed_at = now
            agent.output_metadata = {**agent.output_metadata, "error_code": reason}
        for task in tasks.values():
            if task.status not in TERMINAL | {"cancelled"}:
                task_state(
                    task,
                    "failed"
                    if task.status == "running"
                    else "cancelled"
                    if cancelled
                    else "blocked",
                )
                task.output_summary = reason
        if run.status not in {"completed", "failed", "cancelled"}:
            execution_state(run, "cancelled" if cancelled else "failed")
            run.completed_at = now

    def cancel(self, run_id: UUID) -> ExecutionResponse:
        with session_scope(self.sessions) as session:
            self.stop_locked(session, run_id, cancelled=True)
        return self.get(run_id)

    def recover_stale(self, seconds: int) -> int:
        """Fail closed, never replay a possibly applied patch or abandon a live lock."""
        cutoff = datetime.now(UTC) - timedelta(seconds=seconds)
        count = 0
        with session_scope(self.sessions) as session:
            identifiers = session.scalars(
                select(ExecutionRun.id)
                .where(
                    or_(
                        and_(
                            ExecutionRun.status.in_(["pending", "running"]),
                            ExecutionRun.updated_at < cutoff,
                        ),
                        ExecutionRun.id.in_(
                            select(AgentRun.execution_run_id).where(
                                AgentRun.status == "running", AgentRun.started_at < cutoff
                            )
                        ),
                    ),
                )
                .limit(100)
                .with_for_update(skip_locked=True)
            ).all()
            for identifier in identifiers:
                _, _, tasks = self._load(session, identifier)
                stale_agent = session.scalar(
                    select(AgentRun.id)
                    .where(
                        AgentRun.execution_run_id == identifier,
                        AgentRun.status == "running",
                        AgentRun.started_at < cutoff,
                    )
                    .limit(1)
                )
                if not any(t.status == "running" for t in tasks.values()) and stale_agent is None:
                    continue
                recent = session.scalar(
                    select(AgentRun.id)
                    .where(
                        AgentRun.execution_run_id == identifier,
                        AgentRun.status == "running",
                        AgentRun.started_at >= cutoff,
                    )
                    .limit(1)
                )
                if recent is None:
                    self.stop_locked(session, identifier, cancelled=False)
                    count += 1
        return count

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
