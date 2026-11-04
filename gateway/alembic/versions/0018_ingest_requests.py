"""ingest_requests idempotency ledger

Revision ID: 0018_ingest_requests
Revises: 0017_evidence_search_bigram

Records one external write intent per (client_id, client_request_id).
The row points at the ArchiveObject and ImportJob created for that intent.
It has no claim, lease, or retry columns: execution stays on ImportJob.

payload_hash is stored so a reused request id with a different body can be
rejected. It is not unique and it is not a request id.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0018_ingest_requests"
down_revision = "0017_evidence_search_bigram"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ingest_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("client_request_id", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("archive_object_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("import_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("result_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('archived','completed','failed')",
            name="ck_ingest_requests_status",
        ),
        sa.CheckConstraint(
            "kind IN ('conversation','messages')",
            name="ck_ingest_requests_kind",
        ),
        sa.CheckConstraint(
            "payload_hash ~ '^[0-9a-f]{64}$'",
            name="ck_ingest_requests_payload_hash",
        ),
        sa.CheckConstraint(
            "length(btrim(client_id)) > 0",
            name="ck_ingest_requests_client_id_present",
        ),
        sa.CheckConstraint(
            "length(btrim(client_request_id)) > 0",
            name="ck_ingest_requests_request_id_present",
        ),
        sa.ForeignKeyConstraint(
            ["archive_object_id"],
            ["archive_objects.id"],
            name="fk_ingest_requests_archive_object",
        ),
        sa.ForeignKeyConstraint(
            ["import_job_id"],
            ["import_jobs.id"],
            name="fk_ingest_requests_import_job",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ingest_requests"),
        sa.UniqueConstraint(
            "client_id",
            "client_request_id",
            name="uq_ingest_requests_client_request",
        ),
    )
    op.create_index(
        "ix_ingest_requests_import_job_id",
        "ingest_requests",
        ["import_job_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_ingest_requests_import_job_id", table_name="ingest_requests")
    op.drop_table("ingest_requests")
