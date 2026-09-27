"""Preserve task rationale, acceptance criteria, and suggested tests."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plan_tasks", sa.Column("rationale", sa.Text(), nullable=True))
    for name in ("acceptance_criteria", "suggested_tests"):
        op.add_column(
            "plan_tasks",
            sa.Column(
                name, postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
            ),
        )
        op.create_check_constraint(f"{name}_array", "plan_tasks", f"jsonb_typeof({name}) = 'array'")


def downgrade() -> None:
    for name in ("acceptance_criteria", "suggested_tests"):
        op.drop_constraint(op.f(f"ck_plan_tasks_{name}_array"), "plan_tasks", type_="check")
        op.drop_column("plan_tasks", name)
    op.drop_column("plan_tasks", "rationale")
