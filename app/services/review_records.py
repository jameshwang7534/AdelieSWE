"""Review snapshots and audit persistence, with mechanical evidence rechecked at completion."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import session_scope
from app.integrations.llm.provider import TokenUsage
from app.models import AgentRun, ImplementationPlan, Issue
from app.orchestration.review_gate import test_evidence
from app.orchestration.state import InvalidTransition
from app.orchestration.transitions import agent_state
from app.schemas.context import IssueContext
from app.schemas.review import ReviewDecision, ReviewInput
from app.schemas.testing import RepositoryTestConfig
from app.services.executions import ExecutionService


@dataclass
class ReviewSnapshot:
    inputs: ReviewInput
    errors: list[str]
    tested_diff_hash: str | None
    evidence_hash: str


class ReviewRecords:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions
        self.execution = ExecutionService(sessions)

    def _load(
        self, session: Session, run_id: UUID, context: IssueContext, required: RepositoryTestConfig
    ) -> ReviewSnapshot:
        run, snapshot, tasks = self.execution._load(session, run_id)
        plan = session.get(ImplementationPlan, run.plan_id)
        issue = session.get(Issue, plan.issue_id) if plan else None
        if (
            issue is None
            or issue.id != context.issue.id
            or issue.repository_id != context.repository.id
        ):
            raise InvalidTransition("review_context_mismatch")
        agents = list(
            session.scalars(
                select(AgentRun)
                .where(
                    AgentRun.execution_run_id == run_id,
                    AgentRun.agent_type.in_(("coding", "debug", "test", "recovery")),
                )
                .order_by(AgentRun.started_at, AgentRun.id)
            )
        )
        evidence, errors, fingerprint = test_evidence(tasks, agents, required)
        if run.status != "completed":
            errors.append("execution_incomplete")
        inputs = ReviewInput(
            context=context,
            plan=snapshot.proposal,
            task_outcomes=self.execution._response(run, snapshot, tasks).tasks,
            full_diff="",
            test_results=evidence,
        )
        serialized = json.dumps(
            {
                "inputs": inputs.model_dump(mode="json"),
                "errors": errors,
                "fingerprint": fingerprint,
                "required": required.model_dump(mode="json"),
            },
            sort_keys=True,
        )
        return ReviewSnapshot(inputs, errors, fingerprint, sha256(serialized.encode()).hexdigest())

    def start(
        self, run_id: UUID, context: IssueContext, required: RepositoryTestConfig, model: str | None
    ) -> tuple[UUID, ReviewSnapshot]:
        with session_scope(self.sessions) as session:
            snapshot = self._load(session, run_id, context, required)
            if (
                session.scalar(
                    select(AgentRun.id).where(
                        AgentRun.execution_run_id == run_id,
                        AgentRun.agent_type == "review",
                        AgentRun.status == "running",
                    )
                )
                is not None
            ):
                raise InvalidTransition("review_already_running")
            agent = AgentRun(
                execution_run_id=run_id,
                agent_type="review",
                status="running",
                model=model,
                started_at=datetime.now(UTC),
                input_metadata={
                    "issue_id": str(context.issue.id),
                    "evidence_hash": snapshot.evidence_hash,
                    "required_commands": list(required.required_commands),
                },
            )
            session.add(agent)
            session.flush()
            return agent.id, snapshot

    def finish(
        self,
        run_id: UUID,
        agent_id: UUID,
        context: IssueContext,
        required: RepositoryTestConfig,
        snapshot: ReviewSnapshot,
        decision: ReviewDecision,
        usage: TokenUsage | None,
        error: str | None = None,
    ) -> ReviewDecision:
        with session_scope(self.sessions) as session:
            current = self._load(session, run_id, context, required)
            agent = session.get(AgentRun, agent_id)
            if agent is None or agent.status != "running" or agent.execution_run_id != run_id:
                raise InvalidTransition("review_state_changed")
            errors = list(decision.mechanical_errors)
            if current.evidence_hash != snapshot.evidence_hash:
                errors.append("execution_changed_during_review")
            decision = decision.model_copy(
                update={
                    "mechanical_errors": tuple(dict.fromkeys(errors)),
                    "approved": decision.approved and not errors and error is None,
                }
            )
            agent_state(agent, "failed" if error else "completed")
            agent.completed_at = datetime.now(UTC)
            agent.output_metadata = {
                "decision": decision.model_dump(mode="json"),
                "error_code": error,
            }
            if usage:
                agent.input_tokens, agent.output_tokens = usage.input_tokens, usage.output_tokens
            return decision
