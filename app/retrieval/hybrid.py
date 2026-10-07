"""Equal-weight reciprocal rank fusion over reusable, repository-scoped retrievers."""

from uuid import UUID

from app.core.timing import observed
from app.retrieval.base import RankedRetriever
from app.schemas.search import HybridSearchResult, SearchRequest, SearchResult


class HybridRetrievalService:
    def __init__(
        self,
        bm25: RankedRetriever,
        vector: RankedRetriever,
        *,
        candidate_limit: int = 50,
        rrf_k: int = 60,
    ) -> None:
        if not 1 <= candidate_limit <= 100 or rrf_k < 1:
            raise ValueError("Invalid hybrid retrieval configuration")
        self.bm25, self.vector = bm25, vector
        self.candidate_limit, self.rrf_k = candidate_limit, rrf_k

    @observed("retrieval.hybrid")
    def search(self, repository_id: UUID, query: str, top_k: int = 10) -> list[HybridSearchResult]:
        request = SearchRequest(query=query, top_k=top_k)
        depth = max(self.candidate_limit, request.top_k)
        sources = (
            self.bm25.search(repository_id, request.query, depth),
            self.vector.search(repository_id, request.query, depth),
        )
        documents: dict[UUID, SearchResult] = {}
        evidence: list[dict[UUID, SearchResult]] = []
        for results in sources:
            unique: dict[UUID, SearchResult] = {}
            for hit in sorted(results, key=lambda hit: hit.rank):
                unique.setdefault(hit.chunk_id, hit)
                documents.setdefault(hit.chunk_id, hit)
            evidence.append(unique)
        fused = []
        for identifier, document in documents.items():
            lexical, semantic = (source.get(identifier) for source in evidence)
            score = sum(
                1.0 / (self.rrf_k + hit.rank) for hit in (lexical, semantic) if hit is not None
            )
            fused.append(
                HybridSearchResult(
                    chunk_id=identifier,
                    file_path=document.file_path,
                    start_line=document.start_line,
                    end_line=document.end_line,
                    content=document.content,
                    snippet=document.content,
                    score=score,
                    rank=0,
                    bm25_rank=lexical.rank if lexical else None,
                    bm25_score=lexical.score if lexical else None,
                    vector_rank=semantic.rank if semantic else None,
                    vector_score=semantic.score if semantic else None,
                )
            )
        fused.sort(
            key=lambda hit: (
                -hit.score,
                hit.file_path,
                hit.start_line,
                hit.end_line,
                str(hit.chunk_id),
            )
        )
        return [
            hit.model_copy(update={"rank": rank})
            for rank, hit in enumerate(fused[: request.top_k], 1)
        ]
