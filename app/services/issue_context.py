"""Bounded issue context assembly using deterministic queries and hybrid retrieval."""

from uuid import UUID

from app.core.config import Settings
from app.retrieval.base import ChunkDocument, RankedRetriever
from app.schemas.context import ContextSnippet, IssueContext, RetrievalEvidence
from app.schemas.search import HybridSearchResult
from app.services.context_queries import issue_queries
from app.services.context_store import ContextStore


class IssueContextNotReady(Exception):
    """The repository has no current successful source index."""


class IssueContextService:
    def __init__(self, store: ContextStore, retriever: RankedRetriever, settings: Settings) -> None:
        self.store, self.retriever, self.settings = store, retriever, settings

    def build(self, issue_id: UUID) -> IssueContext:
        limits = self.settings
        seed = self.store.load_issue(issue_id, limits.context_max_issue_chars)
        if seed.repository.index_status != "ready":
            raise IssueContextNotReady
        repository_id = seed.repository.id
        queries = issue_queries(
            seed.issue.title,
            seed.issue.body,
            limits.context_max_queries,
            limits.context_query_chars,
        )
        evidence: dict[UUID, list[RetrievalEvidence]] = {}
        for query in queries:
            for hit in self.retriever.search(repository_id, query, limits.context_max_chunks)[
                : limits.context_max_chunks
            ]:
                entries = evidence.setdefault(hit.chunk_id, [])
                if any(entry.query == query for entry in entries):
                    continue
                entries.append(
                    RetrievalEvidence(
                        query=query,
                        rank=hit.rank,
                        score=hit.score,
                        bm25_rank=hit.bm25_rank if isinstance(hit, HybridSearchResult) else None,
                        bm25_score=hit.bm25_score if isinstance(hit, HybridSearchResult) else None,
                        vector_rank=hit.vector_rank
                        if isinstance(hit, HybridSearchResult)
                        else None,
                        vector_score=hit.vector_score
                        if isinstance(hit, HybridSearchResult)
                        else None,
                    )
                )
        documents = {
            doc.id: doc
            for doc in self.store.chunks(
                repository_id, list(evidence), limits.context_max_code_chars
            )
        }
        ordered = sorted(
            documents.values(),
            key=lambda doc: (
                -max(entry.score for entry in evidence[doc.id]),
                min(entry.rank for entry in evidence[doc.id]),
                doc.file_path,
                doc.start_line,
                str(doc.id),
            ),
        )
        snippets: list[ContextSnippet] = []
        included: set[UUID] = set()
        files: set[str] = set()
        chars = 0
        limited = seed.issue.truncated or len(documents) != len(evidence)

        def include(doc: ChunkDocument, neighbor_of: UUID | None = None) -> None:
            nonlocal chars, limited
            if doc.id in included:
                return
            if (
                len(snippets) >= limits.context_max_chunks
                or (doc.file_path not in files and len(files) >= limits.context_max_files)
                or chars + len(doc.content) > limits.context_max_code_chars
            ):
                limited = True
                return
            snippets.append(
                ContextSnippet(
                    chunk_id=doc.id,
                    file_path=doc.file_path,
                    start_line=doc.start_line,
                    end_line=doc.end_line,
                    content=doc.content,
                    retrieval=evidence.get(doc.id, []),
                    neighbor_of=neighbor_of,
                )
            )
            included.add(doc.id)
            files.add(doc.file_path)
            chars += len(doc.content)

        for doc in ordered:
            include(doc)
        if limits.context_include_neighbors:
            for doc in ordered:
                if doc.id not in included or len(snippets) >= limits.context_max_chunks:
                    continue
                for neighbor in self.store.neighbors(
                    repository_id, doc, limits.context_max_code_chars
                ):
                    include(neighbor, doc.id)
        return IssueContext(
            **seed.model_dump(),
            queries=queries,
            relevant_files=sorted(files),
            snippets=snippets,
            code_chars=chars,
            limited=limited,
        )
