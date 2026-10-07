"""Atomic repository-scoped chunk reconciliation; no retrieval or model calls."""

from collections.abc import Iterator
from pathlib import Path
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, defer, sessionmaker

from app.core.timing import observed
from app.db.session import session_scope
from app.indexing.chunking import Chunk, chunk_source
from app.indexing.scanner import SourceFile
from app.models import CodeChunk, Repository
from app.services.workspace import WorkspaceService


class IndexingError(Exception):
    pass


class Scanner(Protocol):
    def scan(self, root: Path) -> Iterator[SourceFile]: ...


class IndexingService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        workspace: WorkspaceService,
        scanner: Scanner,
        *,
        max_lines: int = 120,
        max_chars: int = 8000,
    ) -> None:
        self.sessions = sessions
        self.workspace = workspace
        self.scanner = scanner
        self.max_lines = max_lines
        self.max_chars = max_chars

    def _status(self, repository_id: UUID, status: str) -> None:
        with session_scope(self.sessions) as session:
            repository = session.get(Repository, repository_id)
            if repository is None:
                raise IndexingError("repository_not_registered")
            if status == "indexing" and repository.local_status != "ready":
                raise IndexingError("workspace_not_ready")
            repository.index_status = status

    @observed("indexing.source")
    def index(self, repository_id: UUID) -> dict[str, str | int]:
        # Hold the same filesystem lock as checkout/reset; no database transaction during scanning.
        with self.workspace.locked_repository(repository_id) as root:
            self._status(repository_id, "indexing")
            try:
                commit = self.workspace.verify(root)
                chunks: list[Chunk] = []
                files = 0
                for source in self.scanner.scan(root):
                    files += 1
                    chunks.extend(chunk_source(source, self.max_lines, self.max_chars))
                if self.workspace.verify(root) != commit:
                    raise IndexingError("workspace_changed")
                self._persist(repository_id, chunks)
                return {
                    "repository_id": str(repository_id),
                    "status": "ready",
                    "files": files,
                    "chunks": len(chunks),
                    "commit": commit,
                }
            except Exception:
                self._status(repository_id, "error")
                raise IndexingError("repository_indexing_failed") from None

    def _persist(self, repository_id: UUID, chunks: list[Chunk]) -> None:
        with session_scope(self.sessions) as session:
            repository = session.scalar(
                select(Repository).where(Repository.id == repository_id).with_for_update()
            )
            if repository is None:
                raise IndexingError("repository_not_registered")
            records = session.scalars(
                select(CodeChunk)
                .where(CodeChunk.repository_id == repository_id)
                .options(defer(CodeChunk.embedding))
                .order_by(CodeChunk.id)
            ).all()
            existing = {}
            for record in records:
                key = (
                    record.file_path,
                    record.start_line,
                    record.end_line,
                    record.content_hash,
                    record.language,
                    record.content,
                )
                if key in existing:
                    session.delete(record)
                else:
                    existing[key] = record
            for chunk in chunks:
                key = (
                    chunk.file_path,
                    chunk.start_line,
                    chunk.end_line,
                    chunk.content_hash,
                    chunk.language,
                    chunk.content,
                )
                if existing.pop(key, None) is None:
                    session.add(
                        CodeChunk(
                            repository_id=repository_id,
                            file_path=chunk.file_path,
                            language=chunk.language,
                            start_line=chunk.start_line,
                            end_line=chunk.end_line,
                            content=chunk.content,
                            content_hash=chunk.content_hash,
                            embedding=None,
                        )
                    )
            for obsolete in existing.values():
                session.delete(obsolete)
            repository.index_status = "ready"
            repository.embedding_status = "pending"
