"""Exact pgvector cosine search over compatible, current, ready embeddings."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.timing import observed
from app.db.session import session_scope
from app.integrations.llm.embeddings import EmbeddingError, EmbeddingProvider, validate_vectors
from app.models import CodeChunk, Repository
from app.models.repository import EMBEDDING_DIMENSION
from app.retrieval.base import RepositoryNotFound
from app.schemas.search import SearchRequest, VectorSearchResult


class VectorRetriever:
    def __init__(self, sessions: sessionmaker[Session], provider: EmbeddingProvider) -> None:
        if provider.dimensions != EMBEDDING_DIMENSION:
            raise EmbeddingError("embedding_dimension_mismatch")
        self.sessions, self.provider = sessions, provider

    @observed("retrieval.vector")
    def search(self, repository_id: UUID, query: str, top_k: int = 10) -> list[VectorSearchResult]:
        request = SearchRequest(query=query, top_k=top_k)
        filters = (
            CodeChunk.repository_id == repository_id,
            CodeChunk.embedding_status == "ready",
            CodeChunk.embedding.is_not(None),
            CodeChunk.embedding_profile == self.provider.profile,
            CodeChunk.embedding_content_hash == CodeChunk.content_hash,
        )
        with session_scope(self.sessions) as session:
            if session.get(Repository, repository_id) is None:
                raise RepositoryNotFound
            if session.scalar(select(CodeChunk.id).where(*filters).limit(1)) is None:
                return []
        vector = validate_vectors(self.provider.embed([request.query]), 1, EMBEDDING_DIMENSION)[0]
        distance = CodeChunk.embedding.cosine_distance(vector).label("distance")
        with session_scope(self.sessions) as session:
            rows = session.execute(
                select(
                    CodeChunk.id,
                    CodeChunk.file_path,
                    CodeChunk.start_line,
                    CodeChunk.end_line,
                    CodeChunk.content,
                    distance,
                )
                .where(*filters)
                .order_by(
                    distance,
                    CodeChunk.file_path,
                    CodeChunk.start_line,
                    CodeChunk.end_line,
                    CodeChunk.id,
                )
                .limit(request.top_k)
            ).all()
        return [
            VectorSearchResult(
                chunk_id=row.id,
                file_path=row.file_path,
                start_line=row.start_line,
                end_line=row.end_line,
                content=row.content,
                distance=max(0.0, min(2.0, row.distance)),
                score=1 - max(0.0, min(2.0, row.distance)),
                rank=rank,
            )
            for rank, row in enumerate(rows, start=1)
        ]
