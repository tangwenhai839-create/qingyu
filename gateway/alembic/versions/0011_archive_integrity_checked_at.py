"""record archive integrity check time

Revision ID: 0011_integrity_checked_at
Revises: 0010_archive_integrity_error
"""
import sqlalchemy as sa
from alembic import op

revision = "0011_integrity_checked_at"
down_revision = "0010_archive_integrity_error"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "archive_objects",
        sa.Column("integrity_checked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("archive_objects", "integrity_checked_at")
