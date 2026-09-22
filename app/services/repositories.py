"""Transactional imports; GitHub requests complete before opening write transactions."""

from uuid import UUID

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import session_scope
from app.integrations.github.client import GitHubClient
from app.models import Issue, Repository
from app.schemas.repositories import IssueResponse, RepositoryResponse


class RecordNotFound(Exception):
    pass


class RepositoryService:
    def __init__(self, sessions: sessionmaker[Session], github: GitHubClient) -> None:
        self.sessions = sessions
        self.github = github

    def register(self, owner: str, name: str) -> RepositoryResponse:
        data = self.github.get_repository(owner, name)
        values = dict(
            github_owner=data.owner.login,
            github_name=data.name,
            clone_url=data.clone_url,
            default_branch=data.default_branch,
        )
        statement = (
            insert(Repository)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[
                    func.lower(Repository.github_owner),
                    func.lower(Repository.github_name),
                ],
                set_=values,
            )
            .returning(Repository)
        )
        with session_scope(self.sessions) as session:
            record = session.scalars(statement).one()
            result = RepositoryResponse.model_validate(record)
        return result

    def get_repository(self, identifier: UUID) -> RepositoryResponse:
        with session_scope(self.sessions) as session:
            record = session.get(Repository, identifier)
            if record is None:
                raise RecordNotFound
            return RepositoryResponse.model_validate(record)

    def import_issue(self, identifier: UUID, number: int) -> IssueResponse:
        repository = self.get_repository(identifier)
        data = self.github.get_issue(repository.github_owner, repository.github_name, number)
        values = dict(title=data.title, body=data.body, state=data.state, source_url=data.html_url)
        statement = (
            insert(Issue)
            .values(
                repository_id=identifier,
                github_issue_number=number,
                **values,
            )
            .on_conflict_do_update(
                index_elements=[Issue.repository_id, Issue.github_issue_number],
                set_=values,
            )
            .returning(Issue)
        )
        with session_scope(self.sessions) as session:
            record = session.scalars(statement).one()
            result = IssueResponse.model_validate(record)
        return result

    def get_issue(self, identifier: UUID) -> IssueResponse:
        with session_scope(self.sessions) as session:
            record = session.get(Issue, identifier)
            if record is None:
                raise RecordNotFound
            return IssueResponse.model_validate(record)
