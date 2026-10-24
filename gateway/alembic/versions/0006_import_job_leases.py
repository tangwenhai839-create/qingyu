"""import job stale lease recovery
Revision ID: 0006_import_job_leases
Revises: 0005_import_job_durability
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_import_job_leases"
down_revision: str | None = "0005_import_job_durability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
def upgrade() -> None:
    op.add_column("import_jobs", sa.Column("attempt_count", sa.Integer(), nullable=True, server_default="0"))
    op.add_column("import_jobs", sa.Column("max_attempts", sa.Integer(), nullable=True, server_default="3"))
    op.add_column("import_jobs", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("import_jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
def downgrade() -> None:
    for c in ("lease_expires_at", "last_heartbeat_at", "max_attempts", "attempt_count"): op.drop_column("import_jobs", c)
