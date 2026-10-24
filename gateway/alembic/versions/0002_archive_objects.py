"""create archive_objects

Revision ID: 0002_archive_objects
Revises: 0001_phase1_baseline
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_archive_objects"
down_revision: str | None = "0001_phase1_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.create_table(
        "archive_objects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("media_type", sa.String(255), nullable=False),
        sa.Column("source_platform", sa.String(100), nullable=False),
        sa.Column("integrity_status", sa.String(32), nullable=False, server_default="verified"),
        sa.Column("first_archived_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.UniqueConstraint("sha256", "size_bytes", name="uq_archive_objects_sha256_size"),
    )
    op.create_index("ix_archive_objects_sha256", "archive_objects", ["sha256"])

def downgrade() -> None:
    op.drop_index("ix_archive_objects_sha256", table_name="archive_objects")
    op.drop_table("archive_objects")
