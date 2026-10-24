"""align import job counters with the application model

Revision ID: 0013_import_job_defaults
Revises: 0012_import_identity_constraints
"""
import sqlalchemy as sa
from alembic import op

revision = "0013_import_job_defaults"
down_revision = "0012_import_identity_constraints"
branch_labels = None
depends_on = None

ZERO_COUNTS = ("discovered_count", "imported_count", "skipped_count", "failed_count", "attempt_count")


def upgrade() -> None:
    for column in ZERO_COUNTS:
        op.execute(sa.text(f"UPDATE import_jobs SET {column} = 0 WHERE {column} IS NULL"))
        op.alter_column("import_jobs", column, existing_type=sa.Integer(), nullable=False, server_default=sa.text("0"))
    op.execute(sa.text("UPDATE import_jobs SET max_attempts = 3 WHERE max_attempts IS NULL"))
    op.alter_column("import_jobs", "max_attempts", existing_type=sa.Integer(), nullable=False, server_default=sa.text("3"))


def downgrade() -> None:
    for column in ("discovered_count", "imported_count", "skipped_count", "failed_count"):
        op.alter_column("import_jobs", column, existing_type=sa.Integer(), nullable=True, server_default=None)
    for column, default in (("attempt_count", "0"), ("max_attempts", "3")):
        op.alter_column("import_jobs", column, existing_type=sa.Integer(), nullable=True, server_default=sa.text(default))
