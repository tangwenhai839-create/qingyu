"""separate archive source observations

Revision ID: 0003_archive_observations
Revises: 0002_archive_objects
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_archive_observations"
down_revision: str | None = "0002_archive_objects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.alter_column("archive_objects", "original_filename", existing_type=sa.Text(), nullable=True)
    op.alter_column("archive_objects", "source_platform", existing_type=sa.String(100), nullable=True)
    op.create_table(
        "archive_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("archive_object_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("archive_objects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_platform", sa.String(100), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=True),
        sa.Column("logical_path", sa.Text(), nullable=True),
        sa.Column("import_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index("ix_archive_observations_archive_object_id", "archive_observations", ["archive_object_id"])
    op.create_index("ix_archive_observations_import_job_id", "archive_observations", ["import_job_id"])

def downgrade() -> None:
    op.drop_index("ix_archive_observations_import_job_id", table_name="archive_observations")
    op.drop_index("ix_archive_observations_archive_object_id", table_name="archive_observations")
    op.drop_table("archive_observations")
