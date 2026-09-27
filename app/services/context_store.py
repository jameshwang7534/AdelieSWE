"""Repository-scoped, bounded reads of issue text and neighboring stored chunks."""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import session_scope
from app.models import CodeChunk, Issue, Repository
from app.retrieval.base import ChunkDocument
from app.schemas.context import ContextIssue, ContextRepository, ContextSeed
from app.services.repositories import RecordNotFound


class ContextStore(Protocol):
    def load_issue(self, issue_id: UUID, max_chars: int) -> ContextSeed: ...
    def chunks(
        self, repository_id: UUID, ids: Sequence[UUID], max_chars: int
    ) -> list[ChunkDocument]: ...
    def neighbors(
        self, repository_id: UUID, chunk: ChunkDocument, max_chars: int
    ) -> list[ChunkDocument]: ...


class PostgresContextStore:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def load_issue(self, issue_id: UUID, max_chars: int) -> ContextSeed:
        with session_scope(self.sessions) as session:
            row = session.execute(
                select(
                    Issue.id,
                    Issue.github_issue_number,
                    func.substr(Issue.title, 1, 512).label("title"),
                    func.length(Issue.title).label("title_length"),
                    func.substr(Issue.body, 1, max_chars).label("body"),
                    func.coalesce(func.length(Issue.body), 0).label("body_length"),
                    Repository.id.label("repository_id"),
                    Repository.github_owner,
                    Repository.github_name,
                    Repository.default_branch,
                    Repository.index_status,
                    Repository.embedding_status,
                )
                .join(Repository, Issue.repository_id == Repository.id)
                .where(Issue.id == issue_id)
            ).one_or_none()
            if row is None:
                raise RecordNotFound
            available = max_chars - len(row.title)
            body = row.body[:available] if row.body is not None else None
            return ContextSeed(
                repository=ContextRepository(
                    id=row.repository_id,
                    github_owner=row.github_owner,
                    github_name=row.github_name,
                    default_branch=row.default_branch,
                    index_status=row.index_status,
                    embedding_status=row.embedding_status,
                ),
                issue=ContextIssue(
                    id=row.id,
                    github_issue_number=row.github_issue_number,
                    title=row.title,
                    body=body,
                    truncated=row.title_length > len(row.title)
                    or row.body_length > len(body or ""),
                ),
            )

    def chunks(
        self, repository_id: UUID, ids: Sequence[UUID], max_chars: int
    ) -> list[ChunkDocument]:
        if not ids:
            return []
        with session_scope(self.sessions) as session:
            rows = session.execute(
                select(
                    CodeChunk.id,
                    CodeChunk.file_path,
                    CodeChunk.start_line,
                    CodeChunk.end_line,
                    func.substr(CodeChunk.content, 1, max_chars + 1).label("content"),
                )
                .where(
                    CodeChunk.repository_id == repository_id,
                    CodeChunk.id.in_(ids),
                    func.length(CodeChunk.file_path) <= 1024,
                )
                .order_by(
                    CodeChunk.file_path, CodeChunk.start_line, CodeChunk.end_line, CodeChunk.id
                )
                .limit(len(ids))
            ).all()
            return [
                ChunkDocument(row.id, row.file_path, row.start_line, row.end_line, row.content)
                for row in rows
            ]

    def neighbors(
        self, repository_id: UUID, chunk: ChunkDocument, max_chars: int
    ) -> list[ChunkDocument]:
        ids: list[UUID] = []
        with session_scope(self.sessions) as session:
            for condition, order in (
                (CodeChunk.end_line < chunk.start_line, CodeChunk.end_line.desc()),
                (CodeChunk.start_line > chunk.end_line, CodeChunk.start_line.asc()),
            ):
                identifier = session.scalar(
                    select(CodeChunk.id)
                    .where(
                        CodeChunk.repository_id == repository_id,
                        CodeChunk.file_path == chunk.file_path,
                        condition,
                    )
                    .order_by(order, CodeChunk.id)
                    .limit(1)
                )
                if identifier is not None:
                    ids.append(identifier)
        return self.chunks(repository_id, ids, max_chars)
