"""Plan and task persistence without planning or scheduling behavior."""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Record


class ImplementationPlan(Record, Base):
    __tablename__ = "implementation_plans"
    issue_id: Mapped[UUID] = mapped_column(ForeignKey("issues.id"), index=True)
    status: Mapped[str] = mapped_column(String(50), server_default="draft")
    summary: Mapped[str | None] = mapped_column(Text)


class PlanTask(Record, Base):
    __tablename__ = "plan_tasks"
    plan_id: Mapped[UUID] = mapped_column(ForeignKey("implementation_plans.id"), index=True)
    task_key: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(50), server_default="pending")
    dependencies: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSONB), default=list, server_default=text("'[]'::jsonb")
    )
    target_files: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSONB), default=list, server_default=text("'[]'::jsonb")
    )
    sequence: Mapped[int] = mapped_column(Integer, server_default="0")
    __table_args__ = (
        UniqueConstraint("plan_id", "task_key"),
        CheckConstraint("sequence >= 0", name="nonnegative_sequence"),
        CheckConstraint("jsonb_typeof(dependencies) = 'array'", name="dependencies_array"),
        CheckConstraint("jsonb_typeof(target_files) = 'array'", name="target_files_array"),
    )
