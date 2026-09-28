"""Freeze validated plan inputs for restartable orchestration."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("execution_runs", sa.Column("plan_snapshot", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("execution_runs", "plan_snapshot")
