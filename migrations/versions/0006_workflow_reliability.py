"""Workflow delivery fencing, retry deadlines and cancellation."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def status_constraint(cancelled: bool) -> None:
    op.drop_constraint("valid_workflow_status", "workflow_runs", type_="check")
    values = "'pending','running','failed','blocked','completed'"
    if cancelled:
        values += ",'cancelled'"
    op.create_check_constraint("valid_workflow_status", "workflow_runs", f"status IN ({values})")


def upgrade() -> None:
    op.add_column(
        "workflow_runs",
        sa.Column("generation", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("workflow_runs", sa.Column("retry_at", sa.DateTime(timezone=True)))
    op.create_index("ix_workflow_runs_retry_at", "workflow_runs", ["retry_at"])
    status_constraint(True)
    op.create_table(
        "worker_deliveries",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("task_name", sa.String(100), nullable=False),
        sa.Column(
            "repository_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result", pg.JSONB(), nullable=False),
        sa.Column("error_code", sa.String(100)),
        sa.CheckConstraint(
            "status IN ('running','completed','failed')", name="valid_delivery_status"
        ),
    )
    op.create_index("ix_worker_deliveries_repository_id", "worker_deliveries", ["repository_id"])
    op.create_index("ix_worker_deliveries_lease_until", "worker_deliveries", ["lease_until"])
    op.execute(
        "CREATE TRIGGER worker_deliveries_updated_at BEFORE UPDATE ON worker_deliveries "
        "FOR EACH ROW EXECUTE FUNCTION set_record_updated_at()"
    )


def downgrade() -> None:
    op.drop_table("worker_deliveries")
    op.execute("UPDATE workflow_runs SET status='blocked' WHERE status='cancelled'")
    op.execute("UPDATE task_executions SET status='failed' WHERE status='cancelled'")
    op.execute("UPDATE execution_runs SET status='failed' WHERE status='cancelled'")
    status_constraint(False)
    op.drop_index("ix_workflow_runs_retry_at", table_name="workflow_runs")
    op.drop_column("workflow_runs", "retry_at")
    op.drop_column("workflow_runs", "generation")
