from sqlalchemy import func, select

from ..models import (
    Attachment,
    AttachmentObservation,
    ConversationNode,
    ConversationObservation,
    ImportBatchCommit,
    ImportJob,
    MappingNodeObservation,
    Message,
    MessageObservation,
    MessageRevision,
)


class ImportedTreeQuery:
    def __init__(self, db): self.db = db

    def _snapshot(self, conversation_id, import_job_id):
        parser_name = self.db.scalar(select(ImportJob.parser_name).where(ImportJob.id == import_job_id))
        if parser_name == "chatgpt-export":
            raise ValueError("real_export_requires_evidence_tree_query")
        statement = select(MessageObservation).join(Message, Message.id == MessageObservation.message_id).where(Message.conversation_id == conversation_id, MessageObservation.import_job_id == import_job_id).order_by(MessageObservation.observation_order)
        return list(self.db.scalars(statement))

    def get_tree(self, conversation_id, import_job_id): return self._snapshot(conversation_id, import_job_id)

    def get_roots(self, conversation_id, import_job_id):
        return [row for row in self._snapshot(conversation_id, import_job_id) if row.parent_message_id is None]

    def get_children(self, conversation_id, import_job_id, message_id):
        return [row for row in self._snapshot(conversation_id, import_job_id) if row.parent_message_id == message_id]

    def get_leaves(self, conversation_id, import_job_id):
        rows = self._snapshot(conversation_id, import_job_id); parents = {row.parent_message_id for row in rows if row.parent_message_id}
        return [row for row in rows if row.message_id not in parents]

    def get_path_to_leaf(self, conversation_id, import_job_id, leaf_message_id):
        rows = {row.message_id: row for row in self._snapshot(conversation_id, import_job_id)}; path=[]; current=leaf_message_id; seen=set()
        while current in rows and current not in seen:
            seen.add(current); path.append(rows[current]); current=rows[current].parent_message_id
        return list(reversed(path))

    def get_branch(self, conversation_id, import_job_id, leaf_message_id):
        return self.get_path_to_leaf(conversation_id, import_job_id, leaf_message_id)

    def get_current_path(self, conversation_id, import_job_id):
        current = next((row for row in self._snapshot(conversation_id, import_job_id) if row.is_current_path), None)
        return [] if current is None else self.get_path_to_leaf(conversation_id, import_job_id, current.message_id)


class EvidenceTreeQuery:
    """Reconstruct one normalized real-export conversation occurrence."""

    def __init__(self, db):
        self.db = db

    def reconstruct(self, import_batch_commit_id):
        batch = self.db.get(ImportBatchCommit, import_batch_commit_id)
        if batch is None:
            return None
        observation = self.db.scalar(select(ConversationObservation).where(
            ConversationObservation.import_batch_commit_id == import_batch_commit_id
        ))
        if observation is None:
            return None
        rows = self.db.execute(
            select(MappingNodeObservation, ConversationNode, Message, MessageRevision, MessageObservation)
            .join(ConversationNode, ConversationNode.id == MappingNodeObservation.conversation_node_id)
            .outerjoin(Message, Message.id == MappingNodeObservation.message_id)
            .outerjoin(MessageRevision, MessageRevision.id == MappingNodeObservation.message_revision_id)
            .outerjoin(
                MessageObservation,
                (MessageObservation.import_job_id == batch.import_job_id)
                & (MessageObservation.message_revision_id == MappingNodeObservation.message_revision_id)
                & (MessageObservation.external_node_id == ConversationNode.external_node_id),
            )
            .where(MappingNodeObservation.conversation_observation_id == observation.id)
            .order_by(MappingNodeObservation.observation_order)
        ).all()
        nodes = []
        for node_observation, node, message, revision, message_observation in rows:
            attachment_rows = []
            if revision is not None:
                attachment_rows = self.db.execute(
                    select(Attachment, AttachmentObservation)
                    .outerjoin(
                        AttachmentObservation,
                        (AttachmentObservation.attachment_id == Attachment.id)
                        & (AttachmentObservation.import_job_id == batch.import_job_id)
                        & (AttachmentObservation.attempt_number == (
                            select(func.min(AttachmentObservation.attempt_number))
                            .where(
                                AttachmentObservation.import_job_id == batch.import_job_id,
                                AttachmentObservation.attachment_id == Attachment.id,
                            )
                            .correlate(Attachment)
                            .scalar_subquery()
                        )),
                    )
                    .where(Attachment.message_revision_id == revision.id)
                    .order_by(Attachment.stable_attachment_key)
                ).all()
            nodes.append({
                "node_id": node.external_node_id,
                "parent_node_id": node_observation.external_parent_node_id,
                "declared_children": node_observation.declared_children_json,
                "message_presence": node_observation.message_presence,
                "message_id": message.external_message_id if message is not None else None,
                "semantic_revision_fingerprint": revision.semantic_revision_fingerprint if revision is not None else None,
                "content_type": revision.content_type if revision is not None else None,
                "raw_create_time": revision.raw_create_time_json if revision is not None else None,
                "raw_update_time": revision.raw_update_time_json if revision is not None else None,
                "attachments": [
                    {
                        "stable_attachment_key": attachment.stable_attachment_key,
                        "descriptor": attachment.descriptor_json,
                        "availability": attachment_observation.availability if attachment_observation else None,
                        "resolution_status": attachment_observation.resolution_status if attachment_observation else None,
                        "archive_object_id": str(attachment_observation.archive_object_id) if attachment_observation and attachment_observation.archive_object_id else None,
                    }
                    for attachment, attachment_observation in attachment_rows
                ],
                "raw_mapping_pointer": node_observation.raw_mapping_pointer,
                "raw_message_pointer": message_observation.raw_message_pointer if message_observation is not None else None,
                "source_node_json": node_observation.source_node_json,
                "source_observation_fingerprint": node_observation.source_observation_fingerprint,
                "canonical_fragment_fingerprint": node_observation.canonical_fragment_fingerprint,
                "semantic_evidence_json": revision.semantic_evidence_json if revision is not None else None,
                "message_observation_json": message_observation.source_observation_json if message_observation is not None else None,
            })
        return {
            "conversation_id": str(observation.conversation_id),
            "source_conversation_index": observation.source_conversation_index,
            "current_node": observation.external_current_node_id,
            "current_node_status": observation.current_node_status,
            "source_member": observation.source_member,
            "source_conversation_json": observation.source_conversation_json,
            "source_observation_fingerprint": observation.source_observation_fingerprint,
            "canonical_fragment_fingerprint": observation.canonical_fragment_fingerprint,
            "nodes": nodes,
        }
