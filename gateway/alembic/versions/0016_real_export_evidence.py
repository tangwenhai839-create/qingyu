"""add real export tree and observation evidence

Revision ID: 0016_real_export_evidence
Revises: 0015_attachment_observations
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0016_real_export_evidence"
down_revision = "0015_attachment_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_import_batch_commits_id_conversation", "import_batch_commits", ["id", "conversation_id"]
    )
    op.create_unique_constraint("uq_messages_conversation_id", "messages", ["conversation_id", "id"])
    op.create_unique_constraint("uq_message_revisions_message_id", "message_revisions", ["message_id", "id"])

    for column in (
        sa.Column("raw_create_time_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("raw_update_time_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("normalized_update_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("create_time_parse_status", sa.String(32), nullable=True),
        sa.Column("update_time_parse_status", sa.String(32), nullable=True),
        sa.Column("time_provenance_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("content_type", sa.String(100), nullable=True),
        sa.Column("semantic_evidence_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("semantic_revision_fingerprint", sa.String(64), nullable=True),
        sa.Column("fingerprint_version", sa.String(32), nullable=True),
    ):
        op.add_column("message_revisions", column)
    op.create_index(
        "ix_message_revisions_semantic_revision_fingerprint",
        "message_revisions",
        ["semantic_revision_fingerprint"],
    )

    for column in (
        sa.Column("raw_message_pointer", sa.Text(), nullable=True),
        sa.Column("source_observation_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source_observation_fingerprint", sa.String(64), nullable=True),
        sa.Column("canonical_fragment_fingerprint", sa.String(64), nullable=True),
        sa.Column("fragment_fingerprint_version", sa.String(32), nullable=True),
    ):
        op.add_column("message_observations", column)

    for column in (
        sa.Column("resolution_status", sa.String(32), nullable=True),
        sa.Column("source_references_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("canonical_fragment_fingerprint", sa.String(64), nullable=True),
        sa.Column("fragment_fingerprint_version", sa.String(32), nullable=True),
    ):
        op.add_column("attachment_observations", column)

    op.create_table(
        "conversation_nodes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_node_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "external_node_id", name="uq_conversation_nodes_conversation_external"),
        sa.UniqueConstraint("conversation_id", "id", name="uq_conversation_nodes_conversation_id"),
        sa.UniqueConstraint("conversation_id", "external_node_id", "id", name="uq_conversation_nodes_external_id_binding"),
    )
    op.create_index("ix_conversation_nodes_conversation_id", "conversation_nodes", ["conversation_id"])

    op.create_table(
        "conversation_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("import_batch_commit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("archive_object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_member", sa.Text(), nullable=False),
        sa.Column("source_conversation_index", sa.Integer(), nullable=False),
        sa.Column("raw_conversation_pointer", sa.Text(), nullable=False),
        sa.Column("external_current_node_id", sa.Text(), nullable=True),
        sa.Column("current_conversation_node_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("current_node_status", sa.String(32), nullable=False),
        sa.Column("source_conversation_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("raw_create_time_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("raw_update_time_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("normalized_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("normalized_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("create_time_parse_status", sa.String(32), nullable=False),
        sa.Column("update_time_parse_status", sa.String(32), nullable=False),
        sa.Column("time_provenance_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_observation_fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_fragment_fingerprint", sa.String(64), nullable=False),
        sa.Column("fragment_fingerprint_version", sa.String(32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("source_conversation_index >= 0", name="ck_conversation_observations_index_nonnegative"),
        sa.CheckConstraint(
            "(current_node_status = 'resolved' AND external_current_node_id IS NOT NULL AND current_conversation_node_id IS NOT NULL) "
            "OR (current_node_status = 'missing' AND external_current_node_id IS NOT NULL AND current_conversation_node_id IS NULL) "
            "OR (current_node_status IN ('absent','null') AND external_current_node_id IS NULL AND current_conversation_node_id IS NULL)",
            name="ck_conversation_observations_current_node_state",
        ),
        sa.ForeignKeyConstraint(["archive_object_id"], ["archive_objects.id"]),
        sa.ForeignKeyConstraint(
            ["import_batch_commit_id", "conversation_id"],
            ["import_batch_commits.id", "import_batch_commits.conversation_id"],
            ondelete="CASCADE",
            name="fk_conversation_observations_batch_conversation",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "external_current_node_id", "current_conversation_node_id"],
            ["conversation_nodes.conversation_id", "conversation_nodes.external_node_id", "conversation_nodes.id"],
            name="fk_conversation_observations_current_node",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("import_batch_commit_id", name="uq_conversation_observations_batch"),
        sa.UniqueConstraint("id", "conversation_id", name="uq_conversation_observations_id_conversation"),
    )
    op.create_index("ix_conversation_observations_archive_object_id", "conversation_observations", ["archive_object_id"])

    op.create_table(
        "mapping_node_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_observation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_conversation_node_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("external_parent_node_id", sa.Text(), nullable=True),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("message_revision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("message_presence", sa.String(16), nullable=False),
        sa.Column("observation_order", sa.Integer(), nullable=False),
        sa.Column("raw_mapping_pointer", sa.Text(), nullable=False),
        sa.Column("declared_children_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source_node_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_observation_fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_fragment_fingerprint", sa.String(64), nullable=False),
        sa.Column("fragment_fingerprint_version", sa.String(32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("observation_order >= 0", name="ck_mapping_node_observations_order_nonnegative"),
        sa.CheckConstraint("message_presence IN ('absent','null','object')", name="ck_mapping_node_observations_message_presence"),
        sa.CheckConstraint("(message_id IS NULL) = (message_revision_id IS NULL)", name="ck_mapping_node_observations_message_revision_pair"),
        sa.CheckConstraint("(external_parent_node_id IS NULL) = (parent_conversation_node_id IS NULL)", name="ck_mapping_node_observations_parent_pair"),
        sa.CheckConstraint(
            "(message_presence = 'object') = (message_id IS NOT NULL AND message_revision_id IS NOT NULL)",
            name="ck_mapping_node_observations_presence_binding",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_observation_id", "conversation_id"],
            ["conversation_observations.id", "conversation_observations.conversation_id"],
            ondelete="CASCADE",
            name="fk_mapping_node_observations_snapshot_conversation",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "conversation_node_id"],
            ["conversation_nodes.conversation_id", "conversation_nodes.id"],
            name="fk_mapping_node_observations_node_conversation",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "external_parent_node_id", "parent_conversation_node_id"],
            ["conversation_nodes.conversation_id", "conversation_nodes.external_node_id", "conversation_nodes.id"],
            name="fk_mapping_node_observations_parent_conversation",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "parent_conversation_node_id"],
            ["conversation_nodes.conversation_id", "conversation_nodes.id"],
            name="fk_mapping_node_observations_parent_scope",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "message_id"],
            ["messages.conversation_id", "messages.id"],
            name="fk_mapping_node_observations_message_conversation",
        ),
        sa.ForeignKeyConstraint(
            ["message_id", "message_revision_id"],
            ["message_revisions.message_id", "message_revisions.id"],
            name="fk_mapping_node_observations_revision_message",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_observation_id", "conversation_node_id", name="uq_mapping_node_observations_snapshot_node"),
        sa.UniqueConstraint("conversation_observation_id", "observation_order", name="uq_mapping_node_observations_snapshot_order"),
    )
    op.create_index("ix_mapping_node_observations_parent", "mapping_node_observations", ["conversation_observation_id", "parent_conversation_node_id"])
    op.create_index("ix_mapping_node_observations_revision", "mapping_node_observations", ["message_revision_id"])


def downgrade() -> None:
    connection = op.get_bind()
    for table in ("mapping_node_observations", "conversation_observations", "conversation_nodes"):
        if connection.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table} LIMIT 1)")).scalar():
            raise RuntimeError("real_export_evidence_downgrade_requires_empty_evidence")
    extension_evidence = (
        (
            "message_revisions",
            (
                "raw_create_time_json", "raw_update_time_json", "normalized_update_timestamp",
                "create_time_parse_status", "update_time_parse_status", "time_provenance_json",
                "content_type", "semantic_evidence_json", "semantic_revision_fingerprint",
                "fingerprint_version",
            ),
        ),
        (
            "message_observations",
            (
                "raw_message_pointer", "source_observation_json", "source_observation_fingerprint",
                "canonical_fragment_fingerprint", "fragment_fingerprint_version",
            ),
        ),
        (
            "attachment_observations",
            (
                "resolution_status", "source_references_json",
                "canonical_fragment_fingerprint", "fragment_fingerprint_version",
            ),
        ),
    )
    for table, columns in extension_evidence:
        predicate = " OR ".join(f"{column} IS NOT NULL" for column in columns)
        if connection.execute(
            sa.text(f"SELECT EXISTS (SELECT 1 FROM {table} WHERE {predicate} LIMIT 1)")
        ).scalar():
            raise RuntimeError("real_export_evidence_downgrade_requires_empty_evidence")

    op.drop_index("ix_mapping_node_observations_revision", table_name="mapping_node_observations")
    op.drop_index("ix_mapping_node_observations_parent", table_name="mapping_node_observations")
    op.drop_table("mapping_node_observations")
    op.drop_index("ix_conversation_observations_archive_object_id", table_name="conversation_observations")
    op.drop_table("conversation_observations")
    op.drop_index("ix_conversation_nodes_conversation_id", table_name="conversation_nodes")
    op.drop_table("conversation_nodes")

    for column in ("fragment_fingerprint_version", "canonical_fragment_fingerprint", "source_references_json", "resolution_status"):
        op.drop_column("attachment_observations", column)
    for column in ("fragment_fingerprint_version", "canonical_fragment_fingerprint", "source_observation_fingerprint", "source_observation_json", "raw_message_pointer"):
        op.drop_column("message_observations", column)
    op.drop_index("ix_message_revisions_semantic_revision_fingerprint", table_name="message_revisions")
    for column in (
        "fingerprint_version", "semantic_revision_fingerprint", "semantic_evidence_json", "content_type",
        "time_provenance_json", "update_time_parse_status", "create_time_parse_status",
        "normalized_update_timestamp", "raw_update_time_json", "raw_create_time_json",
    ):
        op.drop_column("message_revisions", column)
    op.drop_constraint("uq_message_revisions_message_id", "message_revisions", type_="unique")
    op.drop_constraint("uq_messages_conversation_id", "messages", type_="unique")
    op.drop_constraint("uq_import_batch_commits_id_conversation", "import_batch_commits", type_="unique")
