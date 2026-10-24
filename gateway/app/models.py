import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .importers.status import PENDING


class ArchiveObject(Base):
    __tablename__ = "archive_objects"
    __table_args__ = (UniqueConstraint("sha256", "size_bytes", name="uq_archive_objects_sha256_size"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    source_platform: Mapped[str | None] = mapped_column(String(100), nullable=True)
    integrity_status: Mapped[str] = mapped_column(String(32), nullable=False, default="verified")
    integrity_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    integrity_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_archived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)

class ArchiveObservation(Base):
    __tablename__ = "archive_observations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    archive_object_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("archive_objects.id", ondelete="CASCADE"), nullable=False, index=True)
    source_platform: Mapped[str] = mapped_column(String(100), nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    source_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    logical_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    import_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("import_jobs.id", ondelete="SET NULL"), nullable=True, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)

class SourceAccount(Base):
    __tablename__ = "source_accounts"
    __table_args__ = (UniqueConstraint("platform", "account_label", name="uq_source_accounts_platform_label"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    platform: Mapped[str] = mapped_column(String(100), nullable=False)
    account_label: Mapped[str] = mapped_column(String(255), nullable=False)
    external_account_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))

class ImportJob(Base):
    __tablename__ = "import_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','completed','failed','partial','cancelled')",
            name="ck_import_jobs_status",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("source_accounts.id"), nullable=False)
    archive_object_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("archive_objects.id"), nullable=False)
    parser_name: Mapped[str] = mapped_column(String(100), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=PENDING)
    result_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discovered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    imported_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default=text("3"))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    first_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    items_discovered: Mapped[int] = mapped_column(Integer, default=0)
    items_imported: Mapped[int] = mapped_column(Integer, default=0)
    items_skipped: Mapped[int] = mapped_column(Integer, default=0)
    items_failed: Mapped[int] = mapped_column(Integer, default=0)
    checkpoint: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    checkpoint_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))

class ImportBatchCommit(Base):
    __tablename__ = "import_batch_commits"
    __table_args__ = (
        UniqueConstraint("import_job_id", "sequence_no", name="uq_import_batch_commits_job_sequence"),
        UniqueConstraint("import_job_id", "batch_key", name="uq_import_batch_commits_job_key"),
        UniqueConstraint("id", "conversation_id", name="uq_import_batch_commits_id_conversation"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    import_job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("import_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    batch_key: Mapped[str] = mapped_column(Text, nullable=False)
    external_conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    parser_name: Mapped[str] = mapped_column(String(100), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(50), nullable=False)
    report_delta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    counts_delta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))

class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("source_account_id", "external_conversation_id", name="uq_conversations_source_external"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("source_accounts.id"), nullable=False)
    external_conversation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))

class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "external_message_id", name="uq_messages_conversation_external"),
        UniqueConstraint("conversation_id", "id", name="uq_messages_conversation_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False)
    external_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))

class MessageRevision(Base):
    __tablename__ = "message_revisions"
    __table_args__ = (
        UniqueConstraint("message_id", "revision_no", name="uq_message_revisions_no"),
        UniqueConstraint("message_id", "revision_fingerprint", name="uq_message_revisions_fingerprint"),
        UniqueConstraint("message_id", "id", name="uq_message_revisions_message_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("messages.id"), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_json: Mapped[dict | list | None] = mapped_column(JSONB, nullable=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_timestamp: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_create_time_json: Mapped[object | None] = mapped_column(JSONB, nullable=True)
    raw_update_time_json: Mapped[object | None] = mapped_column(JSONB, nullable=True)
    normalized_update_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    create_time_parse_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    update_time_parse_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    time_provenance_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    semantic_evidence_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    semantic_revision_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    fingerprint_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revision_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    archive_object_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("archive_objects.id"), nullable=True)
    raw_json_pointer: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

class MessageObservation(Base):
    __tablename__ = "message_observations"
    __table_args__ = (UniqueConstraint("import_job_id", "message_revision_id", "external_node_id", name="uq_message_observations_job_revision_node"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    import_job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("import_jobs.id"), nullable=False)
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("messages.id"), nullable=False)
    message_revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("message_revisions.id"), nullable=False)
    external_node_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_parent_node_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("messages.id"), nullable=True)
    branch_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_current_path: Mapped[bool] = mapped_column(default=False)
    observation_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_mapping_pointer: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_message_pointer: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    source_observation_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    source_observation_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    canonical_fragment_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fragment_fingerprint_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (
        UniqueConstraint(
            "message_revision_id",
            "stable_attachment_key",
            name="uq_attachments_revision_stable_key",
        ),
        CheckConstraint("length(btrim(stable_attachment_key)) > 0", name="ck_attachments_stable_key_not_blank"),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="ck_attachments_size_nonnegative"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    message_revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("message_revisions.id"), nullable=False, index=True)
    stable_attachment_key: Mapped[str] = mapped_column(Text, nullable=False)
    external_attachment_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    descriptor_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class AttachmentObservation(Base):
    __tablename__ = "attachment_observations"
    __table_args__ = (
        UniqueConstraint(
            "import_job_id",
            "attempt_number",
            "attachment_id",
            name="uq_attachment_observations_job_attempt_attachment",
        ),
        CheckConstraint("attempt_number > 0", name="ck_attachment_observations_attempt_positive"),
        CheckConstraint(
            "availability IN ('available','missing','referenced_only','failed')",
            name="ck_attachment_observations_availability",
        ),
        CheckConstraint(
            "observed_size_bytes IS NULL OR observed_size_bytes >= 0",
            name="ck_attachment_observations_size_nonnegative",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    import_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("import_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    attachment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("attachments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    availability: Mapped[str] = mapped_column(String(32), nullable=False)
    archive_object_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("archive_objects.id"), nullable=True, index=True
    )
    source_member: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachment_member_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_json_pointer: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_media_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    observed_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    resolution_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_references_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    canonical_fragment_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fragment_fingerprint_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class ConversationNode(Base):
    __tablename__ = "conversation_nodes"
    __table_args__ = (
        UniqueConstraint("conversation_id", "external_node_id", name="uq_conversation_nodes_conversation_external"),
        UniqueConstraint("conversation_id", "id", name="uq_conversation_nodes_conversation_id"),
        UniqueConstraint("conversation_id", "external_node_id", "id", name="uq_conversation_nodes_external_id_binding"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    external_node_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


class ConversationObservation(Base):
    __tablename__ = "conversation_observations"
    __table_args__ = (
        UniqueConstraint("import_batch_commit_id", name="uq_conversation_observations_batch"),
        UniqueConstraint("id", "conversation_id", name="uq_conversation_observations_id_conversation"),
        ForeignKeyConstraint(
            ["import_batch_commit_id", "conversation_id"],
            ["import_batch_commits.id", "import_batch_commits.conversation_id"],
            ondelete="CASCADE",
            name="fk_conversation_observations_batch_conversation",
        ),
        ForeignKeyConstraint(
            ["conversation_id", "external_current_node_id", "current_conversation_node_id"],
            ["conversation_nodes.conversation_id", "conversation_nodes.external_node_id", "conversation_nodes.id"],
            name="fk_conversation_observations_current_node",
        ),
        CheckConstraint("source_conversation_index >= 0", name="ck_conversation_observations_index_nonnegative"),
        CheckConstraint(
            "(current_node_status = 'resolved' AND external_current_node_id IS NOT NULL AND current_conversation_node_id IS NOT NULL) "
            "OR (current_node_status = 'missing' AND external_current_node_id IS NOT NULL AND current_conversation_node_id IS NULL) "
            "OR (current_node_status IN ('absent','null') AND external_current_node_id IS NULL AND current_conversation_node_id IS NULL)",
            name="ck_conversation_observations_current_node_state",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    import_batch_commit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    archive_object_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("archive_objects.id"), nullable=False, index=True)
    source_member: Mapped[str] = mapped_column(Text, nullable=False)
    source_conversation_index: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_conversation_pointer: Mapped[str] = mapped_column(Text, nullable=False)
    external_current_node_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_conversation_node_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    current_node_status: Mapped[str] = mapped_column(String(32), nullable=False)
    source_conversation_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    raw_create_time_json: Mapped[object | None] = mapped_column(JSONB, nullable=True)
    raw_update_time_json: Mapped[object | None] = mapped_column(JSONB, nullable=True)
    normalized_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    normalized_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    create_time_parse_status: Mapped[str] = mapped_column(String(32), nullable=False)
    update_time_parse_status: Mapped[str] = mapped_column(String(32), nullable=False)
    time_provenance_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    source_observation_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_fragment_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    fragment_fingerprint_version: Mapped[str] = mapped_column(String(32), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


class MappingNodeObservation(Base):
    __tablename__ = "mapping_node_observations"
    __table_args__ = (
        UniqueConstraint("conversation_observation_id", "conversation_node_id", name="uq_mapping_node_observations_snapshot_node"),
        UniqueConstraint("conversation_observation_id", "observation_order", name="uq_mapping_node_observations_snapshot_order"),
        ForeignKeyConstraint(
            ["conversation_observation_id", "conversation_id"],
            ["conversation_observations.id", "conversation_observations.conversation_id"],
            ondelete="CASCADE",
            name="fk_mapping_node_observations_snapshot_conversation",
        ),
        ForeignKeyConstraint(
            ["conversation_id", "conversation_node_id"],
            ["conversation_nodes.conversation_id", "conversation_nodes.id"],
            name="fk_mapping_node_observations_node_conversation",
        ),
        ForeignKeyConstraint(
            ["conversation_id", "external_parent_node_id", "parent_conversation_node_id"],
            ["conversation_nodes.conversation_id", "conversation_nodes.external_node_id", "conversation_nodes.id"],
            name="fk_mapping_node_observations_parent_conversation",
        ),
        ForeignKeyConstraint(
            ["conversation_id", "parent_conversation_node_id"],
            ["conversation_nodes.conversation_id", "conversation_nodes.id"],
            name="fk_mapping_node_observations_parent_scope",
        ),
        ForeignKeyConstraint(
            ["conversation_id", "message_id"],
            ["messages.conversation_id", "messages.id"],
            name="fk_mapping_node_observations_message_conversation",
        ),
        ForeignKeyConstraint(
            ["message_id", "message_revision_id"],
            ["message_revisions.message_id", "message_revisions.id"],
            name="fk_mapping_node_observations_revision_message",
        ),
        CheckConstraint("observation_order >= 0", name="ck_mapping_node_observations_order_nonnegative"),
        CheckConstraint("message_presence IN ('absent','null','object')", name="ck_mapping_node_observations_message_presence"),
        CheckConstraint("(message_id IS NULL) = (message_revision_id IS NULL)", name="ck_mapping_node_observations_message_revision_pair"),
        CheckConstraint("(external_parent_node_id IS NULL) = (parent_conversation_node_id IS NULL)", name="ck_mapping_node_observations_parent_pair"),
        CheckConstraint(
            "(message_presence = 'object') = (message_id IS NOT NULL AND message_revision_id IS NOT NULL)",
            name="ck_mapping_node_observations_presence_binding",
        ),
        Index("ix_mapping_node_observations_parent", "conversation_observation_id", "parent_conversation_node_id"),
        Index("ix_mapping_node_observations_revision", "message_revision_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_observation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_node_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    parent_conversation_node_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    external_parent_node_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    message_revision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    message_presence: Mapped[str] = mapped_column(String(16), nullable=False)
    observation_order: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_mapping_pointer: Mapped[str] = mapped_column(Text, nullable=False)
    declared_children_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    source_node_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source_observation_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_fragment_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    fragment_fingerprint_version: Mapped[str] = mapped_column(String(32), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
