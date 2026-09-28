"""Atomic coding claims and audit records, separate from filesystem/LLM operations."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from app.db.session import session_scope
from app.integrations.llm.provider import TokenUsage
from app.models import AgentRun, ImplementationPlan, Issue
from app.orchestration.state import InvalidTransition
from app.schemas.coding import CodingInput, DependencyOutcome
from app.schemas.context import IssueContext
from app.services.executions import ExecutionService
from app.services.repositories import RecordNotFound


class CodingRecords:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions
        self.execution = ExecutionService(sessions)

    def start(
        self, run_id: UUID, task_id: UUID, context: IssueContext, model: str | None
    ) -> tuple[UUID, CodingInput]:
        with session_scope(self.sessions) as session:
            run, snapshot, tasks = self.execution._load(session, run_id)
            target = next((item for item in tasks.values() if item.id == task_id), None)
            if target is None:
                raise RecordNotFound
            plan = session.get(ImplementationPlan, run.plan_id)
            issue = session.get(Issue, plan.issue_id) if plan else None
            if (
                issue is None
                or issue.id != context.issue.id
                or issue.repository_id != context.repository.id
            ):
                raise InvalidTransition("coding_context_mismatch")
            proposal = next(
                item
                for item in snapshot.proposal.tasks
                if snapshot.task_ids[item.task_key] == target.plan_task_id
            )
            if target.status != "queued" or any(
                tasks[key].status != "completed" for key in proposal.dependencies
            ):
                raise InvalidTransition("coding_task_not_ready")
            inputs = CodingInput(
                task=proposal,
                context=context,
                dependency_outcomes=[
                    DependencyOutcome(
                        task_key=key,
                        status=tasks[key].status,
                        summary=(tasks[key].output_summary or "")[:4000],
                    )
                    for key in proposal.dependencies
                ],
                workspace_status="not_inspected",
            )
            target.status = "running"
            target.output_summary = "coding_in_progress"
            self.execution._advance(run, snapshot, tasks)
            agent = AgentRun(
                execution_run_id=run_id,
                task_execution_id=task_id,
                agent_type="coding",
                status="running",
                model=model,
                started_at=datetime.now(UTC),
                input_metadata={
                    "issue_id": str(issue.id),
                    "task_key": proposal.task_key,
                    "context_chunks": len(context.snippets),
                },
            )
            session.add(agent)
            session.flush()
            return agent.id, inputs

    def finish(
        self,
        run_id: UUID,
        task_id: UUID,
        agent_id: UUID,
        *,
        success: bool,
        metadata: dict[str, object],
        usage: TokenUsage | None = None,
    ) -> None:
        with session_scope(self.sessions) as session:
            run, snapshot, tasks = self.execution._load(session, run_id)
            target = next((item for item in tasks.values() if item.id == task_id), None)
            agent = session.get(AgentRun, agent_id)
            if target is None or agent is None:
                raise RecordNotFound
            if (
                target.status != "running"
                or agent.status != "running"
                or agent.execution_run_id != run_id
                or agent.task_execution_id != task_id
            ):
                raise InvalidTransition("coding_task_state_changed")
            agent.status = "completed" if success else "failed"
            agent.completed_at = datetime.now(UTC)
            agent.output_metadata = metadata
            if usage is not None:
                agent.input_tokens, agent.output_tokens = usage.input_tokens, usage.output_tokens
            target.output_summary = (
                "patch_applied_awaiting_validation" if success else "coding_failed"
            )
            if not success:
                target.status = "failed"
            self.execution._advance(run, snapshot, tasks)
