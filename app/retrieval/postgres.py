"""Load one repository's current committed corpus without retrieving embedding columns."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import session_scope
from app.models import CodeChunk, Repository
from app.retrieval.base import ChunkDocument, RepositoryNotFound


class PostgresChunkSource:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def load(self, repository_id: UUID) -> list[ChunkDocument]:
        # One statement gives a consistent corpus snapshot and distinguishes missing from empty.
        statement = (
            select(
                Repository.id.label("repository_id"),
                CodeChunk.id.label("chunk_id"),
                CodeChunk.file_path,
                CodeChunk.start_line,
                CodeChunk.end_line,
                CodeChunk.content,
            )
            .outerjoin(CodeChunk, CodeChunk.repository_id == Repository.id)
            .where(Repository.id == repository_id)
        )
        with session_scope(self.sessions) as session:
            rows = session.execute(statement).all()
        if not rows:
            raise RepositoryNotFound
        return [
            ChunkDocument(row.chunk_id, row.file_path, row.start_line, row.end_line, row.content)
            for row in rows
            if row.chunk_id is not None
        ]
