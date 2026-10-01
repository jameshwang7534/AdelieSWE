"""Transactional test claims and completion; no database transaction spans containers."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import session_scope
from app.models import AgentRun, ImplementationPlan, Issue, Repository
from app.orchestration.state import InvalidTransition
from app.schemas.context import ContextRepository
from app.schemas.testing import RepositoryTestConfig, TestInput, TestReport
from app.services.executions import ExecutionService
from app.services.recovery_records import active_recovery
from app.services.repositories import RecordNotFound


class TestRecords:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions
        self.execution = ExecutionService(sessions)

    def start(
        self,
        run_id: UUID,
        task_id: UUID,
        config: RepositoryTestConfig,
        recovery_id: UUID | None = None,
    ) -> tuple[UUID, TestInput]:
        with session_scope(self.sessions) as session:
            run, snapshot, tasks = self.execution._load(session, run_id)
            target = next((item for item in tasks.values() if item.id == task_id), None)
            if target is None:
                raise RecordNotFound
            recovery = active_recovery(session, task_id)
            if (recovery is not None and recovery.id != recovery_id) or (
                recovery_id is not None and recovery is None
            ):
                raise InvalidTransition("recovery_owns_tests")
            if recovery and recovery.input_metadata["required_commands"] != list(
                config.required_commands
            ):
                raise InvalidTransition("recovery_test_policy_changed")
            if (
                target.status != "running"
                or target.output_summary != "patch_applied_awaiting_validation"
            ):
                raise InvalidTransition("task_not_awaiting_tests")
            coding = session.scalar(
                select(AgentRun)
                .where(
                    AgentRun.task_execution_id == task_id,
                    AgentRun.execution_run_id == run_id,
                    AgentRun.agent_type.in_(("coding", "debug") if recovery else ("coding",)),
                    AgentRun.status == "completed",
                )
                .order_by(AgentRun.started_at.desc(), AgentRun.id.desc())
            )
            plan = session.get(ImplementationPlan, run.plan_id)
            issue = session.get(Issue, plan.issue_id) if plan else None
            repo = session.get(Repository, issue.repository_id) if issue else None
            if coding is None or repo is None:
                raise InvalidTransition("coding_result_required")
            task = next(
                t
                for t in snapshot.proposal.tasks
                if snapshot.task_ids[t.task_key] == target.plan_task_id
            )
            proposal = coding.output_metadata.get("proposal", {})
            suggested = proposal.get("tests_to_run", []) if isinstance(proposal, dict) else []
            diff = coding.output_metadata.get("git_diff")
            if (
                not isinstance(diff, str)
                or not isinstance(suggested, list)
                or any(not isinstance(item, str) for item in suggested)
            ):
                raise InvalidTransition("invalid_coding_result")
            inputs = TestInput(
                repository=ContextRepository(
                    id=repo.id,
                    github_owner=repo.github_owner,
                    github_name=repo.github_name,
                    default_branch=repo.default_branch,
                    index_status=repo.index_status,
                    embedding_status=repo.embedding_status,
                ),
                task=task,
                coding_diff=diff,
                suggested_tests=tuple(suggested),
                configuration=config,
            )
            target.output_summary = "tests_running"
            agent = AgentRun(
                execution_run_id=run_id,
                task_execution_id=task_id,
                agent_type="test",
                status="running",
                started_at=datetime.now(UTC),
                input_metadata={
                    "required_commands": list(config.required_commands),
                    "coding_agent_run_id": str(coding.id),
                    "task_key": task.task_key,
                    "recovery_id": str(recovery_id) if recovery_id else None,
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
        report: TestReport,
        error: str | None = None,
        recovery_id: UUID | None = None,
        workspace_diff_hash: str | None = None,
    ) -> None:
        with session_scope(self.sessions) as session:
            run, snapshot, tasks = self.execution._load(session, run_id)
            target = next((item for item in tasks.values() if item.id == task_id), None)
            agent = session.get(AgentRun, agent_id)
            if (
                target is None
                or agent is None
                or target.status != "running"
                or target.output_summary != "tests_running"
                or agent.status != "running"
                or agent.execution_run_id != run_id
                or agent.task_execution_id != task_id
            ):
                raise InvalidTransition("test_state_changed")
            passed = error is None and report.passed and bool(report.results)
            recovery = active_recovery(session, task_id)
            if (recovery is not None and recovery.id != recovery_id) or (
                recovery_id is not None and recovery is None
            ):
                raise InvalidTransition("recovery_owns_tests")
            agent.status = "completed" if passed else "failed"
            agent.completed_at = datetime.now(UTC)
            agent.output_metadata = {
                "report": report.model_dump(mode="json"),
                "error_code": error,
                "workspace_diff_hash": workspace_diff_hash,
            }
            target.status = "completed" if passed else "failed"
            target.output_summary = "required_tests_passed" if passed else "required_tests_failed"
            if recovery:
                target.status = "running"
                target.output_summary = "recovery_tests_passed" if passed else "recovery_pending"
            self.execution._advance(run, snapshot, tasks)
