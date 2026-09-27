"""Reusable retrieval contracts independent of HTTP and database clients."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.schemas.search import SearchResult


@dataclass(frozen=True)
class ChunkDocument:
    id: UUID
    file_path: str
    start_line: int
    end_line: int
    content: str


class RepositoryNotFound(Exception):
    pass


class ChunkSource(Protocol):
    def load(self, repository_id: UUID) -> Sequence[ChunkDocument]: ...


class Retriever(Protocol):
    def search(self, repository_id: UUID, query: str, top_k: int = 10) -> list[SearchResult]: ...


class RankedRetriever(Protocol):
    """Common interface for lexical, vector, and default hybrid retrieval."""

    def search(
        self, repository_id: UUID, query: str, top_k: int = 10
    ) -> Sequence[SearchResult]: ...
