import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConversationObservationSummary(PublicModel):
    conversation_observation_id: uuid.UUID
    import_batch_commit_id: uuid.UUID
    conversation_id: uuid.UUID
    source_account_id: uuid.UUID
    archive_object_id: uuid.UUID
    source_platform: str
    parser_name: str
    parser_contract_version: str
    committed_at: datetime
    observed_at: datetime
    observed_title: str | None
    normalized_created_at: datetime | None
    normalized_updated_at: datetime | None
    create_time_parse_status: str
    update_time_parse_status: str
    current_conversation_node_id: uuid.UUID | None
    current_node_status: str
    source_observation_fingerprint: str
    canonical_fragment_fingerprint: str
    fragment_fingerprint_version: str


class AttachmentAttemptEvidence(PublicModel):
    attachment_observation_id: uuid.UUID
    attempt_number: int
    availability: str
    archive_object_id: uuid.UUID | None
    observed_filename: str | None
    observed_media_type: str | None
    observed_size_bytes: int | None
    resolution_status: str | None
    canonical_fragment_fingerprint: str | None
    fragment_fingerprint_version: str | None
    observed_at: datetime


class AttachmentEvidence(PublicModel):
    attachment_id: uuid.UUID
    filename: str | None
    media_type: str | None
    size_bytes: int | None


class MessageRevisionEvidence(PublicModel):
    message_id: uuid.UUID
    message_revision_id: uuid.UUID
    revision_no: int
    role: str
    content_text: str | None
    content_json: JsonValue | None
    content_type: str | None
    model: str | None
    raw_timestamp: str | None
    normalized_timestamp: datetime | None
    normalized_update_timestamp: datetime | None
    create_time_parse_status: str | None
    update_time_parse_status: str | None
    semantic_revision_fingerprint: str | None
    fingerprint_version: str | None
    revision_fingerprint: str | None
    message_observation_id: uuid.UUID | None
    source_observation_fingerprint: str | None
    canonical_fragment_fingerprint: str | None
    fragment_fingerprint_version: str | None
    attachments: list[AttachmentEvidence]


class NodeEvidence(PublicModel):
    mapping_node_observation_id: uuid.UUID
    conversation_node_id: uuid.UUID
    parent_conversation_node_id: uuid.UUID | None
    declared_child_conversation_node_ids: list[uuid.UUID] | None
    message_presence: Literal["absent", "null", "object"]
    observation_order: int
    source_observation_fingerprint: str
    canonical_fragment_fingerprint: str
    fragment_fingerprint_version: str
    message: MessageRevisionEvidence | None


class ObservationPage(PublicModel):
    contract_version: Literal["evidence-query-v1"] = "evidence-query-v1"
    meaning: Literal["gateway_recorded_historical_evidence"] = (
        "gateway_recorded_historical_evidence"
    )
    consistency: Literal["bounded_high_water_enumeration_v1"] = (
        "bounded_high_water_enumeration_v1"
    )
    items: list[ConversationObservationSummary]
    next_cursor: str | None
    limit: int = Field(ge=1, le=100)


class NodePage(PublicModel):
    contract_version: Literal["evidence-query-v1"] = "evidence-query-v1"
    meaning: Literal["gateway_recorded_historical_evidence"] = (
        "gateway_recorded_historical_evidence"
    )
    consistency: Literal["bounded_high_water_enumeration_v1"] = (
        "bounded_high_water_enumeration_v1"
    )
    observation: ConversationObservationSummary
    items: list[NodeEvidence]
    next_cursor: str | None
    limit: int = Field(ge=1, le=100)


class AttachmentObservationPage(PublicModel):
    contract_version: Literal["evidence-query-v1"] = "evidence-query-v1"
    meaning: Literal["gateway_recorded_historical_evidence"] = (
        "gateway_recorded_historical_evidence"
    )
    consistency: Literal["bounded_high_water_enumeration_v1"] = (
        "bounded_high_water_enumeration_v1"
    )
    attachment: AttachmentEvidence
    items: list[AttachmentAttemptEvidence]
    next_cursor: str | None
    limit: int = Field(ge=1, le=100)
