"""Durable workflow stages, idempotency keys, and bounded worker claims."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("request", pg.JSONB(), nullable=False),
        sa.Column("data", pg.JSONB(), nullable=False),
        sa.Column("history", pg.JSONB(), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("claim_token", sa.Uuid()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(100)),
        sa.CheckConstraint("attempts >= 0", name="nonnegative_workflow_attempts"),
        sa.CheckConstraint(
            "status IN ('pending','running','failed','blocked','completed')",
            name="valid_workflow_status",
        ),
    )
    op.create_index("ix_workflow_runs_status", "workflow_runs", ["status"])
    op.create_index("ix_workflow_runs_lease_until", "workflow_runs", ["lease_until"])
    op.execute(
        "CREATE TRIGGER workflow_runs_updated_at BEFORE UPDATE ON workflow_runs "
        "FOR EACH ROW EXECUTE FUNCTION set_record_updated_at()"
    )


def downgrade() -> None:
    op.drop_table("workflow_runs")
