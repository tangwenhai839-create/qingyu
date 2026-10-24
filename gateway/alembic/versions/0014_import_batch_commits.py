"""add durable import batch ledger and monotonic checkpoint sequence

Revision ID: 0014_import_batch_commits
Revises: 0013_import_job_defaults
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014_import_batch_commits"
down_revision = "0013_import_job_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_import_jobs_status",
        "import_jobs",
        "status IN ('pending','running','completed','failed','partial','cancelled')",
    )
    op.add_column(
        "import_jobs",
        sa.Column("checkpoint_sequence", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_table(
        "import_batch_commits",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("import_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("batch_key", sa.Text(), nullable=False),
        sa.Column("external_conversation_id", sa.Text(), nullable=False),
        sa.Column("parser_name", sa.String(length=100), nullable=False),
        sa.Column("contract_version", sa.String(length=50), nullable=False),
        sa.Column("report_delta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("counts_delta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["import_job_id"], ["import_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("import_job_id", "batch_key", name="uq_import_batch_commits_job_key"),
        sa.UniqueConstraint("import_job_id", "sequence_no", name="uq_import_batch_commits_job_sequence"),
    )
    op.create_index("ix_import_batch_commits_import_job_id", "import_batch_commits", ["import_job_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_import_batch_commits_import_job_id", table_name="import_batch_commits")
    op.drop_table("import_batch_commits")
    op.drop_column("import_jobs", "checkpoint_sequence")
    op.drop_constraint("ck_import_jobs_status", "import_jobs", type_="check")
