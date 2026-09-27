"""Track embedding provenance and resumable generation state."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("repositories", "code_chunks"):
        op.add_column(
            table,
            sa.Column("embedding_status", sa.String(50), nullable=False, server_default="pending"),
        )
    op.add_column("code_chunks", sa.Column("embedding_profile", sa.String(64), nullable=True))
    op.add_column("code_chunks", sa.Column("embedding_content_hash", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("code_chunks", "embedding_content_hash")
    op.drop_column("code_chunks", "embedding_profile")
    for table in ("code_chunks", "repositories"):
        op.drop_column(table, "embedding_status")
