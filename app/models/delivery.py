"""Durable identities for standalone repository Celery deliveries."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Record


class WorkerDelivery(Record, Base):
    __tablename__ = "worker_deliveries"
    task_name: Mapped[str] = mapped_column(String(100))
    repository_id: Mapped[UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(20))
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    result: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(100))
    __table_args__ = (
        CheckConstraint("status IN ('running','completed','failed')", name="valid_delivery_status"),
    )
