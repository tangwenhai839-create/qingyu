import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, or_, select

from ..models import (
    Attachment,
    AttachmentObservation,
    ConversationNode,
    ConversationObservation,
    ImportBatchCommit,
    ImportJob,
    MappingNodeObservation,
    MessageObservation,
    MessageRevision,
    SourceAccount,
)


@dataclass(frozen=True)
class QueryPage:
    items: list[dict]
    has_more: bool
    upper_bound: tuple[datetime, uuid.UUID] | None = None
    context: dict | None = None


@dataclass(frozen=True)
class AttachmentObservationQueryPage:
    attachment: dict
    items: list[dict]
    has_more: bool
    upper_bound: tuple[int, datetime, uuid.UUID] | None


class EvidenceIntegrityError(RuntimeError):
    pass


def observed_title(source_conversation_json: object) -> str | None:
    if not isinstance(source_conversation_json, dict):
        return None
    title = source_conversation_json.get("title")
    return title if isinstance(title, str) else None


class EvidenceQueryService:
    """Return allow-listed evidence projections; never return ORM entities."""

    def __init__(self, db):
        self.db = db

    @staticmethod
    def _observation_filters(source_account_id, conversation_id):
        filters = []
        if source_account_id is not None:
            filters.append(ImportJob.source_account_id == source_account_id)
        if conversation_id is not None:
            filters.append(ConversationObservation.conversation_id == conversation_id)
        return filters

    def list_conversation_observations(
        self,
        *,
        limit: int,
        source_account_id: uuid.UUID | None = None,
        conversation_id: uuid.UUID | None = None,
        upper_bound: tuple[datetime, uuid.UUID] | None = None,
        after: tuple[datetime, uuid.UUID] | None = None,
    ) -> QueryPage:
        filters = self._observation_filters(source_account_id, conversation_id)
        joins = (
            select(ConversationObservation, ImportBatchCommit, ImportJob, SourceAccount)
            .join(
                ImportBatchCommit,
                ImportBatchCommit.id == ConversationObservation.import_batch_commit_id,
            )
            .join(ImportJob, ImportJob.id == ImportBatchCommit.import_job_id)
            .join(SourceAccount, SourceAccount.id == ImportJob.source_account_id)
        )
        if upper_bound is None:
            upper_row = self.db.execute(
                select(ImportBatchCommit.committed_at, ConversationObservation.id)
                .join(
                    ConversationObservation,
                    ConversationObservation.import_batch_commit_id == ImportBatchCommit.id,
                )
                .join(ImportJob, ImportJob.id == ImportBatchCommit.import_job_id)
                .where(*filters)
                .order_by(ImportBatchCommit.committed_at.desc(), ConversationObservation.id.desc())
                .limit(1)
            ).first()
            upper_bound = tuple(upper_row) if upper_row is not None else None
        if upper_bound is None:
            return QueryPage(items=[], has_more=False, upper_bound=None)
        upper_time, upper_id = upper_bound
        filters.append(
            or_(
                ImportBatchCommit.committed_at < upper_time,
                and_(
                    ImportBatchCommit.committed_at == upper_time,
                    ConversationObservation.id <= upper_id,
                ),
            )
        )
        if after is not None:
            after_time, after_id = after
            filters.append(
                or_(
                    ImportBatchCommit.committed_at > after_time,
                    and_(
                        ImportBatchCommit.committed_at == after_time,
                        ConversationObservation.id > after_id,
                    ),
                )
            )
        rows = self.db.execute(
            joins.where(*filters)
            .order_by(ImportBatchCommit.committed_at, ConversationObservation.id)
            .limit(limit + 1)
        ).all()
        has_more = len(rows) > limit
        items = []
        for observation, batch, job, account in rows[:limit]:
            items.append(
                {
                    "conversation_observation_id": observation.id,
                    "import_batch_commit_id": batch.id,
                    "conversation_id": observation.conversation_id,
                    "source_account_id": job.source_account_id,
                    "archive_object_id": observation.archive_object_id,
                    "source_platform": account.platform,
                    "parser_name": batch.parser_name,
                    "parser_contract_version": batch.contract_version,
                    "committed_at": batch.committed_at,
                    "observed_at": observation.observed_at,
                    "observed_title": observed_title(observation.source_conversation_json),
                    "normalized_created_at": observation.normalized_created_at,
                    "normalized_updated_at": observation.normalized_updated_at,
                    "create_time_parse_status": observation.create_time_parse_status,
                    "update_time_parse_status": observation.update_time_parse_status,
                    "current_conversation_node_id": observation.current_conversation_node_id,
                    "current_node_status": observation.current_node_status,
                    "source_observation_fingerprint": observation.source_observation_fingerprint,
                    "canonical_fragment_fingerprint": observation.canonical_fragment_fingerprint,
                    "fragment_fingerprint_version": observation.fragment_fingerprint_version,
                }
            )
        return QueryPage(items=items, has_more=has_more, upper_bound=upper_bound)

    def get_observation_context(self, observation_id: uuid.UUID):
        return self.db.execute(
            select(ConversationObservation, ImportBatchCommit, ImportJob, SourceAccount)
            .join(
                ImportBatchCommit,
                ImportBatchCommit.id == ConversationObservation.import_batch_commit_id,
            )
            .join(ImportJob, ImportJob.id == ImportBatchCommit.import_job_id)
            .join(SourceAccount, SourceAccount.id == ImportJob.source_account_id)
            .where(ConversationObservation.id == observation_id)
        ).first()

    def list_observation_nodes(
        self,
        *,
        observation_id: uuid.UUID,
        limit: int,
        after: tuple[int, uuid.UUID] | None = None,
    ) -> QueryPage | None:
        context = self.get_observation_context(observation_id)
        if context is None:
            return None
        observation, batch, job, account = context
        filters = [MappingNodeObservation.conversation_observation_id == observation_id]
        if after is not None:
            after_order, after_id = after
            filters.append(
                or_(
                    MappingNodeObservation.observation_order > after_order,
                    and_(
                        MappingNodeObservation.observation_order == after_order,
                        MappingNodeObservation.id > after_id,
                    ),
                )
            )
        rows = self.db.execute(
            select(
                MappingNodeObservation,
                ConversationNode,
                MessageRevision,
                MessageObservation,
            )
            .join(ConversationNode, ConversationNode.id == MappingNodeObservation.conversation_node_id)
            .outerjoin(
                MessageRevision,
                MessageRevision.id == MappingNodeObservation.message_revision_id,
            )
            .outerjoin(
                MessageObservation,
                (MessageObservation.import_job_id == batch.import_job_id)
                & (MessageObservation.message_revision_id == MappingNodeObservation.message_revision_id)
                & (MessageObservation.external_node_id == ConversationNode.external_node_id),
            )
            .where(*filters)
            .order_by(MappingNodeObservation.observation_order, MappingNodeObservation.id)
            .limit(limit + 1)
        ).all()
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        declared_external_ids = {
            child_id
            for node_observation, _, _, _ in page_rows
            if node_observation.declared_children_json is not None
            for child_id in node_observation.declared_children_json
            if isinstance(child_id, str)
        }
        child_node_ids: dict[str, uuid.UUID] = {}
        if declared_external_ids:
            child_nodes = list(
                self.db.scalars(
                    select(ConversationNode).where(
                        ConversationNode.conversation_id == observation.conversation_id,
                        ConversationNode.external_node_id.in_(declared_external_ids),
                    )
                )
            )
            child_node_ids = {item.external_node_id: item.id for item in child_nodes}
        revision_ids = [
            revision.id for _, _, revision, _ in page_rows if revision is not None
        ]
        attachments_by_revision: dict[uuid.UUID, list[Attachment]] = {}
        if revision_ids:
            attachments = list(
                self.db.scalars(
                    select(Attachment)
                    .where(Attachment.message_revision_id.in_(revision_ids))
                    .order_by(Attachment.message_revision_id, Attachment.stable_attachment_key, Attachment.id)
                )
            )
            for attachment in attachments:
                attachments_by_revision.setdefault(attachment.message_revision_id, []).append(attachment)
        items = []
        for node_observation, node, revision, message_observation in page_rows:
            declared_children = None
            if node_observation.declared_children_json is not None:
                declared_children = []
                for external_id in node_observation.declared_children_json:
                    if not isinstance(external_id, str) or external_id not in child_node_ids:
                        raise EvidenceIntegrityError("declared_child_reference_missing")
                    declared_children.append(child_node_ids[external_id])
            attachments = []
            if revision is not None:
                for attachment in attachments_by_revision.get(revision.id, []):
                    attachments.append(
                        {
                            "attachment_id": attachment.id,
                            "filename": attachment.filename,
                            "media_type": attachment.media_type,
                            "size_bytes": attachment.size_bytes,
                        }
                    )
            message = None
            if revision is not None:
                message = {
                    "message_id": node_observation.message_id,
                    "message_revision_id": revision.id,
                    "revision_no": revision.revision_no,
                    "role": revision.role,
                    "content_text": revision.content_text,
                    "content_json": revision.content_json,
                    "content_type": revision.content_type,
                    "model": revision.model,
                    "raw_timestamp": revision.raw_timestamp,
                    "normalized_timestamp": revision.normalized_timestamp,
                    "normalized_update_timestamp": revision.normalized_update_timestamp,
                    "create_time_parse_status": revision.create_time_parse_status,
                    "update_time_parse_status": revision.update_time_parse_status,
                    "semantic_revision_fingerprint": revision.semantic_revision_fingerprint,
                    "fingerprint_version": revision.fingerprint_version,
                    "revision_fingerprint": revision.revision_fingerprint,
                    "message_observation_id": (
                        message_observation.id if message_observation is not None else None
                    ),
                    "source_observation_fingerprint": (
                        message_observation.source_observation_fingerprint
                        if message_observation is not None
                        else None
                    ),
                    "canonical_fragment_fingerprint": (
                        message_observation.canonical_fragment_fingerprint
                        if message_observation is not None
                        else None
                    ),
                    "fragment_fingerprint_version": (
                        message_observation.fragment_fingerprint_version
                        if message_observation is not None
                        else None
                    ),
                    "attachments": attachments,
                }
            items.append(
                {
                    "mapping_node_observation_id": node_observation.id,
                    "conversation_node_id": node.id,
                    "parent_conversation_node_id": node_observation.parent_conversation_node_id,
                    "declared_child_conversation_node_ids": declared_children,
                    "message_presence": node_observation.message_presence,
                    "observation_order": node_observation.observation_order,
                    "source_observation_fingerprint": node_observation.source_observation_fingerprint,
                    "canonical_fragment_fingerprint": node_observation.canonical_fragment_fingerprint,
                    "fragment_fingerprint_version": node_observation.fragment_fingerprint_version,
                    "message": message,
                }
            )
        summary = {
            "conversation_observation_id": observation.id,
            "import_batch_commit_id": batch.id,
            "conversation_id": observation.conversation_id,
            "source_account_id": job.source_account_id,
            "archive_object_id": observation.archive_object_id,
            "source_platform": account.platform,
            "parser_name": batch.parser_name,
            "parser_contract_version": batch.contract_version,
            "committed_at": batch.committed_at,
            "observed_at": observation.observed_at,
            "observed_title": observed_title(observation.source_conversation_json),
            "normalized_created_at": observation.normalized_created_at,
            "normalized_updated_at": observation.normalized_updated_at,
            "create_time_parse_status": observation.create_time_parse_status,
            "update_time_parse_status": observation.update_time_parse_status,
            "current_conversation_node_id": observation.current_conversation_node_id,
            "current_node_status": observation.current_node_status,
            "source_observation_fingerprint": observation.source_observation_fingerprint,
            "canonical_fragment_fingerprint": observation.canonical_fragment_fingerprint,
            "fragment_fingerprint_version": observation.fragment_fingerprint_version,
        }
        return QueryPage(
            items=items,
            has_more=has_more,
            context=summary,
        )

    def list_attachment_observations(
        self,
        *,
        attachment_id: uuid.UUID,
        conversation_observation_id: uuid.UUID,
        limit: int,
        upper_bound: tuple[int, datetime, uuid.UUID] | None = None,
        after: tuple[int, datetime, uuid.UUID] | None = None,
    ) -> AttachmentObservationQueryPage | None:
        context = self.db.execute(
            select(Attachment, ConversationObservation, ImportBatchCommit, ImportJob)
            .join(
                MappingNodeObservation,
                MappingNodeObservation.message_revision_id == Attachment.message_revision_id,
            )
            .join(
                ConversationObservation,
                ConversationObservation.id == MappingNodeObservation.conversation_observation_id,
            )
            .join(
                ImportBatchCommit,
                ImportBatchCommit.id == ConversationObservation.import_batch_commit_id,
            )
            .join(ImportJob, ImportJob.id == ImportBatchCommit.import_job_id)
            .where(
                Attachment.id == attachment_id,
                ConversationObservation.id == conversation_observation_id,
            )
            .limit(1)
        ).first()
        if context is None:
            return None
        attachment, _observation, _batch, job = context
        base_filters = [
            AttachmentObservation.attachment_id == attachment_id,
            AttachmentObservation.import_job_id == job.id,
        ]
        if upper_bound is None:
            upper_row = self.db.execute(
                select(
                    AttachmentObservation.attempt_number,
                    AttachmentObservation.observed_at,
                    AttachmentObservation.id,
                )
                .where(*base_filters)
                .order_by(
                    AttachmentObservation.attempt_number.desc(),
                    AttachmentObservation.observed_at.desc(),
                    AttachmentObservation.id.desc(),
                )
                .limit(1)
            ).first()
            upper_bound = tuple(upper_row) if upper_row is not None else None
        if upper_bound is None:
            return AttachmentObservationQueryPage(
                attachment={
                    "attachment_id": attachment.id,
                    "filename": attachment.filename,
                    "media_type": attachment.media_type,
                    "size_bytes": attachment.size_bytes,
                },
                items=[],
                has_more=False,
                upper_bound=None,
            )

        def greater_than(key):
            attempt, observed_at, item_id = key
            return or_(
                AttachmentObservation.attempt_number > attempt,
                and_(
                    AttachmentObservation.attempt_number == attempt,
                    or_(
                        AttachmentObservation.observed_at > observed_at,
                        and_(
                            AttachmentObservation.observed_at == observed_at,
                            AttachmentObservation.id > item_id,
                        ),
                    ),
                ),
            )

        def less_than_or_equal(key):
            attempt, observed_at, item_id = key
            return or_(
                AttachmentObservation.attempt_number < attempt,
                and_(
                    AttachmentObservation.attempt_number == attempt,
                    or_(
                        AttachmentObservation.observed_at < observed_at,
                        and_(
                            AttachmentObservation.observed_at == observed_at,
                            AttachmentObservation.id <= item_id,
                        ),
                    ),
                ),
            )

        filters = [*base_filters, less_than_or_equal(upper_bound)]
        if after is not None:
            filters.append(greater_than(after))
        rows = list(
            self.db.scalars(
                select(AttachmentObservation)
                .where(*filters)
                .order_by(
                    AttachmentObservation.attempt_number,
                    AttachmentObservation.observed_at,
                    AttachmentObservation.id,
                )
                .limit(limit + 1)
            )
        )
        items = [
            {
                "attachment_observation_id": item.id,
                "attempt_number": item.attempt_number,
                "availability": item.availability,
                "archive_object_id": item.archive_object_id,
                "observed_filename": item.observed_filename,
                "observed_media_type": item.observed_media_type,
                "observed_size_bytes": item.observed_size_bytes,
                "resolution_status": item.resolution_status,
                "canonical_fragment_fingerprint": item.canonical_fragment_fingerprint,
                "fragment_fingerprint_version": item.fragment_fingerprint_version,
                "observed_at": item.observed_at,
            }
            for item in rows[:limit]
        ]
        return AttachmentObservationQueryPage(
            attachment={
                "attachment_id": attachment.id,
                "filename": attachment.filename,
                "media_type": attachment.media_type,
                "size_bytes": attachment.size_bytes,
            },
            items=items,
            has_more=len(rows) > limit,
            upper_bound=upper_bound,
        )
