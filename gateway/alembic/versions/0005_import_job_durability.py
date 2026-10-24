"""durable import job fields and archive observation FK

Revision ID: 0005_import_job_durability
Revises: 493a55a2df21
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_import_job_durability"
down_revision: str | None = "493a55a2df21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    for name, typ in (("started_at", sa.DateTime(timezone=True)), ("finished_at", sa.DateTime(timezone=True)), ("discovered_count", sa.Integer()), ("imported_count", sa.Integer()), ("skipped_count", sa.Integer()), ("failed_count", sa.Integer())):
        op.add_column("import_jobs", sa.Column(name, typ, nullable=True))
    op.create_foreign_key("fk_archive_observations_import_job", "archive_observations", "import_jobs", ["import_job_id"], ["id"], ondelete="SET NULL")

def downgrade() -> None:
    op.drop_constraint("fk_archive_observations_import_job", "archive_observations", type_="foreignkey")
    for name in ("failed_count", "skipped_count", "imported_count", "discovered_count", "finished_at", "started_at"):
        op.drop_column("import_jobs", name)
