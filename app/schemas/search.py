"""Lexical search request and result contracts."""

from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=10, ge=1, le=100)

    @field_validator("query")
    @classmethod
    def nonblank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value


class SearchResult(BaseModel):
    chunk_id: UUID
    file_path: str
    start_line: int
    end_line: int
    content: str
    score: float
    rank: int


class SearchResponse(BaseModel):
    results: list[SearchResult]


class VectorSearchResult(SearchResult):
    distance: float


class VectorSearchResponse(BaseModel):
    results: list[VectorSearchResult]


class HybridSearchResult(SearchResult):
    """Score/rank are the final fused values; missing source evidence is null."""

    snippet: str
    bm25_rank: int | None = None
    bm25_score: float | None = None
    vector_rank: int | None = None
    vector_score: float | None = None


class HybridSearchResponse(BaseModel):
    results: list[HybridSearchResult]
