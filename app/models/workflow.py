"""Durable end-to-end workflow checkpoint and single-stage ownership."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Record


class WorkflowRun(Record, Base):
    __tablename__ = "workflow_runs"
    request: Mapped[dict[str, object]] = mapped_column(JSONB)
    data: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    history: Mapped[list[dict[str, object]]] = mapped_column(JSONB, default=list)
    stage: Mapped[str] = mapped_column(String(32), default="repository")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    claim_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    generation: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(100))
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="nonnegative_workflow_attempts"),
        CheckConstraint(
            "status IN ('pending','running','failed','blocked','completed','cancelled')",
            name="valid_workflow_status",
        ),
    )
