"""Revalidate review/test evidence and journal publication before any external writes."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import session_scope
from app.models import AgentRun, ExecutionRun, Issue, PullRequest, Repository
from app.orchestration.transitions import agent_state
from app.pull_requests.contracts import Publication, PublicationError, PublishedPR
from app.schemas.context import IssueContext
from app.schemas.review import ReviewDecision
from app.schemas.testing import RepositoryTestConfig
from app.services.review_records import ReviewRecords, ReviewSnapshot


@dataclass
class PublicationEvidence:
    snapshot: ReviewSnapshot
    decision: ReviewDecision
    review_id: UUID
    owner: str
    repository: str
    source: str
    base: str
    issue_number: int
    issue_url: str
    title: str


class PublicationRecords:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def check(
        self, run_id: UUID, context: IssueContext, required: RepositoryTestConfig
    ) -> PublicationEvidence:
        with session_scope(self.sessions) as session:
            snapshot = ReviewRecords(self.sessions)._load(session, run_id, context, required)
            review = session.scalars(
                select(AgentRun)
                .where(
                    AgentRun.execution_run_id == run_id,
                    AgentRun.agent_type == "review",
                )
                .order_by(AgentRun.started_at.desc(), AgentRun.id.desc())
            ).first()
            if snapshot.errors or review is None or review.status != "completed":
                raise PublicationError("publication_requirements_not_met")
            decision = ReviewDecision.model_validate(review.output_metadata.get("decision"))
            if (
                not decision.approved
                or decision.mechanical_errors
                or not decision.review
                or not decision.review.approved
                or any(f.severity == "blocking" for f in decision.review.findings)
                or review.input_metadata.get("evidence_hash") != snapshot.evidence_hash
                or decision.diff_hash != snapshot.tested_diff_hash
                or review.output_metadata.get("error_code") is not None
            ):
                raise PublicationError("publication_review_not_approved")
            repository = session.get(Repository, context.repository.id)
            issue = session.get(Issue, context.issue.id)
            if repository is None or issue is None:
                raise PublicationError("publication_repository_missing")
            return PublicationEvidence(
                snapshot,
                decision,
                review.id,
                repository.github_owner,
                repository.github_name,
                repository.clone_url,
                repository.default_branch,
                issue.github_issue_number,
                issue.source_url or "",
                issue.title,
            )

    def journal(self, publication: Publication) -> UUID:
        with session_scope(self.sessions) as session:
            run = session.scalar(
                select(ExecutionRun)
                .where(ExecutionRun.id == publication.execution_id)
                .with_for_update()
            )
            if run is None:
                raise PublicationError("publication_execution_missing")
            agent = session.scalar(
                select(AgentRun).where(
                    AgentRun.execution_run_id == run.id,
                    AgentRun.agent_type == "pull_request",
                )
            )
            if agent is None:
                agent = AgentRun(
                    execution_run_id=run.id,
                    agent_type="pull_request",
                    status="running",
                    started_at=datetime.now(UTC),
                    input_metadata=publication.model_dump(mode="json"),
                )
                session.add(agent)
            elif agent.input_metadata != publication.model_dump(mode="json"):
                raise PublicationError("publication_snapshot_changed")
            run.branch_name = publication.branch
            session.flush()
            return agent.id

    def finish(
        self, publication: Publication, journal_id: UUID, remote: PublishedPR
    ) -> PublishedPR:
        with session_scope(self.sessions) as session:
            session.execute(
                select(ExecutionRun)
                .where(ExecutionRun.id == publication.execution_id)
                .with_for_update()
            )
            record = session.scalar(
                select(PullRequest).where(PullRequest.execution_run_id == publication.execution_id)
            )
            if record is None:
                record = PullRequest(
                    execution_run_id=publication.execution_id,
                    github_pr_number=remote.number,
                    github_url=remote.html_url,
                    status=remote.state,
                )
                session.add(record)
            elif record.github_pr_number != remote.number or record.github_url != remote.html_url:
                raise PublicationError("publication_pr_collision")
            else:
                record.status = remote.state
            agent = session.get(AgentRun, journal_id)
            if agent is None:
                raise PublicationError("publication_journal_missing")
            agent_state(agent, "completed")
            agent.completed_at = datetime.now(UTC)
            agent.output_metadata = {
                **agent.output_metadata,
                "pull_request": remote.model_dump(mode="json"),
            }
        return remote

    def failed(self, journal_id: UUID, code: str) -> None:
        with session_scope(self.sessions) as session:
            agent = session.get(AgentRun, journal_id)
            if agent is None:
                raise PublicationError("publication_journal_missing")
            # Append safe codes without discarding earlier attempt history.
            history = agent.output_metadata.get("errors", [])
            errors = list(history) if isinstance(history, list) else []
            # A later remote collision does not undo an already published local record.
            if agent.status != "completed":
                agent_state(agent, "failed")
            agent.output_metadata = {**agent.output_metadata, "errors": [*errors, code]}
