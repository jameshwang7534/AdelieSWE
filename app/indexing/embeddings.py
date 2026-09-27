"""Resumable batch embedding generation; provider calls run outside database transactions."""

from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import session_scope
from app.integrations.llm.embeddings import EmbeddingError, EmbeddingProvider, validate_vectors
from app.models import CodeChunk, Repository
from app.models.repository import EMBEDDING_DIMENSION
from app.services.workspace import WorkspaceService


class EmbeddingService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        workspace: WorkspaceService,
        provider: EmbeddingProvider,
        batch_size: int = 16,
    ) -> None:
        if provider.dimensions != EMBEDDING_DIMENSION or batch_size < 1:
            raise EmbeddingError("embedding_dimension_mismatch")
        self.sessions, self.workspace, self.provider = sessions, workspace, provider
        self.batch_size = batch_size

    def generate(self, repository_id: UUID) -> dict[str, str | int]:
        with self.workspace.locked_repository(repository_id):
            with session_scope(self.sessions) as session:
                repository = session.get(Repository, repository_id)
                if repository is None or repository.index_status != "ready":
                    raise EmbeddingError("index_not_ready")
                repository.embedding_status = "embedding"
            completed = 0
            active: list[UUID] = []
            try:
                while True:
                    with session_scope(self.sessions) as session:
                        rows = session.execute(
                            select(
                                CodeChunk.id,
                                CodeChunk.content,
                                CodeChunk.content_hash,
                            )
                            .where(
                                CodeChunk.repository_id == repository_id,
                                or_(
                                    CodeChunk.embedding.is_(None),
                                    CodeChunk.embedding_status != "ready",
                                    CodeChunk.embedding_profile.is_distinct_from(
                                        self.provider.profile
                                    ),
                                    CodeChunk.embedding_content_hash.is_distinct_from(
                                        CodeChunk.content_hash
                                    ),
                                ),
                            )
                            .order_by(CodeChunk.id)
                            .limit(self.batch_size)
                        ).all()
                        active = [row.id for row in rows]
                        if active:
                            session.execute(
                                update(CodeChunk)
                                .where(CodeChunk.id.in_(active))
                                .values(
                                    embedding_status="embedding",
                                )
                            )
                    if not rows:
                        break
                    vectors = validate_vectors(
                        self.provider.embed([row.content for row in rows]),
                        len(rows),
                        EMBEDDING_DIMENSION,
                    )
                    with session_scope(self.sessions) as session:
                        for row, vector in zip(rows, vectors, strict=True):
                            saved = session.execute(
                                update(CodeChunk)
                                .where(
                                    CodeChunk.id == row.id,
                                    CodeChunk.content_hash == row.content_hash,
                                )
                                .values(
                                    embedding=vector,
                                    embedding_status="ready",
                                    embedding_profile=self.provider.profile,
                                    embedding_content_hash=row.content_hash,
                                )
                                .returning(CodeChunk.id)
                            ).scalar_one_or_none()
                            if saved is None:
                                raise EmbeddingError("chunk_changed_during_embedding")
                    completed += len(rows)
                    active = []
                with session_scope(self.sessions) as session:
                    session.execute(
                        update(Repository)
                        .where(Repository.id == repository_id)
                        .values(
                            embedding_status="ready",
                        )
                    )
            except Exception:
                with session_scope(self.sessions) as session:
                    if active:
                        session.execute(
                            update(CodeChunk)
                            .where(CodeChunk.id.in_(active))
                            .values(
                                embedding_status="error",
                            )
                        )
                    session.execute(
                        update(Repository)
                        .where(Repository.id == repository_id)
                        .values(
                            embedding_status="error",
                        )
                    )
                raise EmbeddingError("embedding_generation_failed") from None
            return {"repository_id": str(repository_id), "status": "ready", "embedded": completed}
