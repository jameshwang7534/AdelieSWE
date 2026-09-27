"""Bounded issue context, with explicit truncation and retrieval provenance."""

from uuid import UUID

from pydantic import BaseModel


class ContextRepository(BaseModel):
    id: UUID
    github_owner: str
    github_name: str
    default_branch: str
    index_status: str
    embedding_status: str


class ContextIssue(BaseModel):
    id: UUID
    github_issue_number: int
    title: str
    body: str | None
    truncated: bool


class ContextSeed(BaseModel):
    repository: ContextRepository
    issue: ContextIssue


class RetrievalEvidence(BaseModel):
    query: str
    rank: int
    score: float
    bm25_rank: int | None = None
    bm25_score: float | None = None
    vector_rank: int | None = None
    vector_score: float | None = None


class ContextSnippet(BaseModel):
    chunk_id: UUID
    file_path: str
    start_line: int
    end_line: int
    content: str
    retrieval: list[RetrievalEvidence]
    neighbor_of: UUID | None = None


class IssueContext(ContextSeed):
    queries: list[str]
    relevant_files: list[str]
    snippets: list[ContextSnippet]
    code_chars: int
    limited: bool
