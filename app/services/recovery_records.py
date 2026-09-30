"""Persistent recovery ownership, budget accounting and final scheduler transitions."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import session_scope
from app.integrations.llm.provider import TokenUsage
from app.models import AgentRun, ImplementationPlan, Issue, TaskExecution
from app.orchestration.state import InvalidTransition
from app.schemas.context import IssueContext
from app.schemas.debugging import DebugAttempt
from app.schemas.planning import PlanTaskProposal
from app.schemas.testing import RepositoryTestConfig
from app.services.executions import ExecutionService


def active_recovery(session: Session, task_id: UUID) -> AgentRun | None:
    return session.scalar(
        select(AgentRun).where(
            AgentRun.task_execution_id == task_id,
            AgentRun.agent_type == "recovery",
            AgentRun.status == "running",
        )
    )


class RecoveryRecords:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions
        self.execution = ExecutionService(sessions)

    def open(
        self,
        run_id: UUID,
        task_id: UUID,
        context: IssueContext,
        config: RepositoryTestConfig,
        limit: int,
    ) -> tuple[UUID, PlanTaskProposal, bool]:
        with session_scope(self.sessions) as session:
            run, snapshot, tasks = self.execution._load(session, run_id)
            target = next((t for t in tasks.values() if t.id == task_id), None)
            if target is None or active_recovery(session, task_id) is not None:
                raise InvalidTransition("recovery_unavailable")
            if target.status != "queued" and not (
                target.status == "running"
                and target.output_summary == "patch_applied_awaiting_validation"
            ):
                raise InvalidTransition("task_not_ready_for_recovery")
            plan = session.get(ImplementationPlan, run.plan_id)
            issue = session.get(Issue, plan.issue_id) if plan else None
            if (
                issue is None
                or issue.id != context.issue.id
                or issue.repository_id != context.repository.id
            ):
                raise InvalidTransition("recovery_context_mismatch")
            task = next(
                t
                for t in snapshot.proposal.tasks
                if snapshot.task_ids[t.task_key] == target.plan_task_id
            )
            agent = AgentRun(
                execution_run_id=run_id,
                task_execution_id=task_id,
                agent_type="recovery",
                status="running",
                started_at=datetime.now(UTC),
                input_metadata={
                    "limit": limit,
                    "required_commands": list(config.required_commands),
                },
            )
            session.add(agent)
            session.flush()
            return agent.id, task, target.status == "queued"

    def _locked(self, session: Session, recovery_id: UUID) -> tuple[AgentRun, TaskExecution]:
        parent = session.get(AgentRun, recovery_id)
        if parent is None:
            raise InvalidTransition("recovery_missing")
        _, _, tasks = self.execution._load(session, parent.execution_run_id)
        session.refresh(parent)
        target = next(t for t in tasks.values() if t.id == parent.task_execution_id)
        if parent.status != "running":
            raise InvalidTransition("recovery_finished")
        return parent, target

    def attempts(self, session: Session, recovery_id: UUID) -> list[AgentRun]:
        return list(
            session.scalars(
                select(AgentRun)
                .where(
                    AgentRun.agent_type == "debug",
                    AgentRun.input_metadata["recovery_id"].as_string() == str(recovery_id),
                )
                .order_by(AgentRun.input_metadata["attempt"].as_integer())
            )
        )

    def claim_debug(self, recovery_id: UUID, model: str | None) -> tuple[UUID, list[DebugAttempt]]:
        with session_scope(self.sessions) as session:
            parent, target = self._locked(session, recovery_id)
            previous = self.attempts(session, recovery_id)
            limit = parent.input_metadata["limit"]
            if (
                not isinstance(limit, int)
                or len(previous) >= limit
                or target.status != "running"
                or target.output_summary != "recovery_pending"
            ):
                raise InvalidTransition("debug_attempt_unavailable")
            history = [
                DebugAttempt(
                    number=i + 1,
                    status=a.status,
                    summary=str(a.output_metadata.get("summary", ""))[:2000],
                    error=str(a.output_metadata["error"])
                    if a.output_metadata.get("error")
                    else None,
                )
                for i, a in enumerate(previous)
            ]
            agent = AgentRun(
                execution_run_id=parent.execution_run_id,
                task_execution_id=target.id,
                agent_type="debug",
                status="running",
                model=model,
                started_at=datetime.now(UTC),
                input_metadata={"recovery_id": str(recovery_id), "attempt": len(previous) + 1},
            )
            session.add(agent)
            target.output_summary = "debug_running"
            session.flush()
            return agent.id, history

    def reserve_patch(self, recovery_id: UUID, agent_id: UUID, fingerprint: str) -> bool:
        with session_scope(self.sessions) as session:
            parent, _ = self._locked(session, recovery_id)
            attempts = self.attempts(session, recovery_id)
            repeated = any(a.output_metadata.get("patch_hash") == fingerprint for a in attempts)
            agent = next(a for a in attempts if a.id == agent_id)
            if agent.status != "running":
                raise InvalidTransition("debug_not_running")
            # Also reject reapplication of the initial coding patch.
            coding = session.scalars(
                select(AgentRun).where(
                    AgentRun.task_execution_id == parent.task_execution_id,
                    AgentRun.agent_type == "coding",
                )
            )
            for record in coding:
                proposal = record.output_metadata.get("proposal")
                if isinstance(proposal, dict) and isinstance(proposal.get("unified_diff"), str):
                    repeated |= sha256(proposal["unified_diff"].encode()).hexdigest() == fingerprint
            agent.output_metadata["patch_hash"] = fingerprint
            return not repeated

    def finish_debug(
        self,
        recovery_id: UUID,
        agent_id: UUID,
        metadata: dict[str, object],
        success: bool,
        usage: TokenUsage | None,
    ) -> None:
        with session_scope(self.sessions) as session:
            _, target = self._locked(session, recovery_id)
            agent = session.get(AgentRun, agent_id)
            if agent is None or agent.status != "running" or agent.task_execution_id != target.id:
                raise InvalidTransition("debug_not_running")
            agent.output_metadata.update(metadata)
            agent.status = "completed" if success else "failed"
            agent.completed_at = datetime.now(UTC)
            if usage:
                agent.input_tokens, agent.output_tokens = usage.input_tokens, usage.output_tokens
            target.output_summary = (
                "patch_applied_awaiting_validation" if success else "recovery_pending"
            )

    def finish(self, recovery_id: UUID, passed: bool, error: str | None) -> None:
        with session_scope(self.sessions) as session:
            parent, target = self._locked(session, recovery_id)
            if passed and (
                target.status != "running" or target.output_summary != "recovery_tests_passed"
            ):
                raise InvalidTransition("tests_not_passed")
            parent.status = "completed" if passed else "failed"
            parent.completed_at = datetime.now(UTC)
            parent.output_metadata = {
                "error": error,
                "debug_attempts": len(self.attempts(session, recovery_id)),
            }
            if not passed:
                target.status = "failed"
                target.output_summary = error or "recovery_failed"
            else:
                target.status = "completed"
                target.output_summary = "required_tests_passed"
            run, snapshot, tasks = self.execution._load(session, parent.execution_run_id)
            self.execution._advance(run, snapshot, tasks)
