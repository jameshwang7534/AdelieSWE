"""Repository, issue, and stored code chunk records (no indexing behavior)."""

from uuid import UUID

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Record

EMBEDDING_DIMENSION = 1536  # Schema contract; change only through a new migration.


class Repository(Record, Base):
    __tablename__ = "repositories"
    github_owner: Mapped[str] = mapped_column(String(255))
    github_name: Mapped[str] = mapped_column(String(255))
    clone_url: Mapped[str] = mapped_column(Text)
    default_branch: Mapped[str] = mapped_column(String(255))
    local_status: Mapped[str] = mapped_column(String(50), server_default="pending")
    index_status: Mapped[str] = mapped_column(String(50), server_default="pending")
    __table_args__ = (
        Index(
            "uq_repositories_github_identity",
            func.lower(github_owner),
            func.lower(github_name),
            unique=True,
        ),
    )


class Issue(Record, Base):
    __tablename__ = "issues"
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    github_issue_number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(50), server_default="open")
    source_url: Mapped[str] = mapped_column(Text)
    __table_args__ = (
        UniqueConstraint("repository_id", "github_issue_number"),
        CheckConstraint("github_issue_number > 0", name="positive_issue_number"),
    )


class CodeChunk(Record, Base):
    __tablename__ = "code_chunks"
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id"), index=True)
    file_path: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(100))
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    embedding: Mapped[list[float] | None] = mapped_column(VECTOR(EMBEDDING_DIMENSION))
    __table_args__ = (
        CheckConstraint("start_line > 0 AND end_line >= start_line", name="valid_line_range"),
    )
