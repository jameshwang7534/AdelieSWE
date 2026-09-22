"""Execution and review records, without worker/agent/GitHub behavior."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Record


class ExecutionRun(Record, Base):
    __tablename__ = "execution_runs"
    plan_id: Mapped[UUID] = mapped_column(ForeignKey("implementation_plans.id"), index=True)
    status: Mapped[str] = mapped_column(String(50), server_default="pending")
    branch_name: Mapped[str | None] = mapped_column(String(255))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("completed_at >= started_at", name="valid_run_times"),)


class TaskExecution(Record, Base):
    __tablename__ = "task_executions"
    execution_run_id: Mapped[UUID] = mapped_column(ForeignKey("execution_runs.id"), index=True)
    plan_task_id: Mapped[UUID] = mapped_column(ForeignKey("plan_tasks.id"), index=True)
    status: Mapped[str] = mapped_column(String(50), server_default="pending")
    attempt: Mapped[int] = mapped_column(Integer, server_default="1")
    output_summary: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        UniqueConstraint("execution_run_id", "plan_task_id", "attempt"),
        CheckConstraint("attempt > 0", name="positive_attempt"),
    )


class AgentRun(Record, Base):
    __tablename__ = "agent_runs"
    execution_run_id: Mapped[UUID] = mapped_column(ForeignKey("execution_runs.id"), index=True)
    task_execution_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("task_executions.id"), index=True
    )
    agent_type: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(50), server_default="pending")
    model: Mapped[str | None] = mapped_column(String(255))
    input_metadata: Mapped[dict[str, object]] = mapped_column(
        MutableDict.as_mutable(JSONB), default=dict, server_default=text("'{}'::jsonb")
    )
    output_metadata: Mapped[dict[str, object]] = mapped_column(
        MutableDict.as_mutable(JSONB), default=dict, server_default=text("'{}'::jsonb")
    )
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cost_usd >= 0", name="nonnegative_usage"
        ),
        CheckConstraint("completed_at >= started_at", name="valid_agent_times"),
        CheckConstraint("jsonb_typeof(input_metadata) = 'object'", name="input_metadata_object"),
        CheckConstraint("jsonb_typeof(output_metadata) = 'object'", name="output_metadata_object"),
    )


class PullRequest(Record, Base):
    __tablename__ = "pull_requests"
    execution_run_id: Mapped[UUID] = mapped_column(ForeignKey("execution_runs.id"), unique=True)
    github_pr_number: Mapped[int] = mapped_column(Integer)
    github_url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(50), server_default="open")
    __table_args__ = (CheckConstraint("github_pr_number > 0", name="positive_pr_number"),)
