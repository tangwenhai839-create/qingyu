import uuid
from datetime import UTC, datetime, timedelta

from app.evidence.query import EvidenceQueryService
from app.models import (
    ArchiveObject,
    Attachment,
    AttachmentObservation,
    Conversation,
    ConversationNode,
    ConversationObservation,
    ImportBatchCommit,
    ImportJob,
    MappingNodeObservation,
    Message,
    MessageObservation,
    MessageRevision,
    SourceAccount,
)
from sqlalchemy.orm import Session


def _fingerprint(character: str) -> str:
    return character * 64


def test_real_postgres_evidence_queries_and_keyset_joins(verified_test_database):
    """Exercise the public query shapes against the allowlisted PostgreSQL service."""
    connection = verified_test_database.connect()
    transaction = connection.begin()
    db = Session(bind=connection)
    try:
        now = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
        archive = ArchiveObject(
            id=uuid.uuid4(),
            sha256=_fingerprint("a"),
            size_bytes=1,
            relative_path=f"test/evidence-{uuid.uuid4()}.zip",
            original_filename="evidence.zip",
            media_type="application/zip",
            integrity_status="verified",
            metadata_json={},
        )
        account = SourceAccount(
            id=uuid.uuid4(),
            platform="chatgpt",
            account_label=f"evidence-test-{uuid.uuid4()}",
            metadata_json={},
        )
        conversations = [
            Conversation(
                id=uuid.uuid4(),
                source_account_id=account.id,
                external_conversation_id=f"evidence-{uuid.uuid4()}",
                title="mutable convenience title",
                metadata_json={},
            )
            for _ in range(2)
        ]
        job = ImportJob(
            id=uuid.uuid4(),
            source_account_id=account.id,
            archive_object_id=archive.id,
            parser_name="chatgpt-export",
            parser_version="test",
            status="completed",
            checkpoint={},
            report_json={},
        )
        db.add_all([archive, account])
        db.flush()
        db.add_all([*conversations, job])
        db.flush()

        batches = []
        observations = []
        for index, conversation in enumerate(conversations):
            batch = ImportBatchCommit(
                id=uuid.uuid4(),
                import_job_id=job.id,
                conversation_id=conversation.id,
                sequence_no=index + 1,
                batch_key=f"evidence-batch-{uuid.uuid4()}",
                external_conversation_id=conversation.external_conversation_id,
                parser_name="chatgpt-export",
                contract_version="1",
                report_delta={},
                counts_delta={},
                committed_at=now + timedelta(seconds=index),
            )
            observation = ConversationObservation(
                id=uuid.uuid4(),
                import_batch_commit_id=batch.id,
                conversation_id=conversation.id,
                archive_object_id=archive.id,
                source_member="conversations.json",
                source_conversation_index=index,
                raw_conversation_pointer=f"/{index}",
                current_node_status="absent",
                source_conversation_json={"title": f"historical-{index}"},
                create_time_parse_status="absent",
                update_time_parse_status="absent",
                time_provenance_json={},
                source_observation_fingerprint=_fingerprint("b"),
                canonical_fragment_fingerprint=_fingerprint("c"),
                fragment_fingerprint_version="1",
                observed_at=now + timedelta(seconds=index),
            )
            batches.append(batch)
            observations.append(observation)
        db.add_all(batches)
        db.flush()
        db.add_all(observations)
        db.flush()

        root = ConversationNode(
            id=uuid.uuid4(),
            conversation_id=conversations[0].id,
            external_node_id="root",
        )
        child = ConversationNode(
            id=uuid.uuid4(),
            conversation_id=conversations[0].id,
            external_node_id="child",
        )
        message = Message(
            id=uuid.uuid4(),
            conversation_id=conversations[0].id,
            external_message_id="message-1",
        )
        revision = MessageRevision(
            id=uuid.uuid4(),
            message_id=message.id,
            revision_no=1,
            role="user",
            content_text="historical user statement",
            content_json={"parts": ["historical user statement"]},
            content_type="text",
            revision_fingerprint=_fingerprint("d"),
        )
        db.add_all([root, child, message])
        db.flush()
        db.add(revision)
        db.flush()
        mapping_root = MappingNodeObservation(
            id=uuid.uuid4(),
            conversation_observation_id=observations[0].id,
            conversation_id=conversations[0].id,
            conversation_node_id=root.id,
            message_id=message.id,
            message_revision_id=revision.id,
            message_presence="object",
            observation_order=0,
            raw_mapping_pointer="/mapping/root",
            declared_children_json=["child"],
            source_node_json={},
            source_observation_fingerprint=_fingerprint("e"),
            canonical_fragment_fingerprint=_fingerprint("f"),
            fragment_fingerprint_version="1",
        )
        mapping_child = MappingNodeObservation(
            id=uuid.uuid4(),
            conversation_observation_id=observations[0].id,
            conversation_id=conversations[0].id,
            conversation_node_id=child.id,
            parent_conversation_node_id=root.id,
            external_parent_node_id="root",
            message_presence="null",
            observation_order=1,
            raw_mapping_pointer="/mapping/child",
            declared_children_json=[],
            source_node_json={},
            source_observation_fingerprint=_fingerprint("1"),
            canonical_fragment_fingerprint=_fingerprint("2"),
            fragment_fingerprint_version="1",
        )
        message_observation = MessageObservation(
            id=uuid.uuid4(),
            import_job_id=job.id,
            message_id=message.id,
            message_revision_id=revision.id,
            external_node_id="root",
            observation_order=0,
            metadata_json={},
            source_observation_fingerprint=_fingerprint("3"),
            canonical_fragment_fingerprint=_fingerprint("4"),
            fragment_fingerprint_version="1",
        )
        attachment = Attachment(
            id=uuid.uuid4(),
            message_revision_id=revision.id,
            stable_attachment_key="member:/private/source.png",
            original_reference="reference:private-source",
            filename="source.png",
            media_type="image/png",
            size_bytes=1,
            descriptor_json={},
        )
        db.add(attachment)
        db.flush()
        db.add_all([mapping_root, mapping_child, message_observation])
        db.flush()
        attempts = [
            AttachmentObservation(
                id=uuid.uuid4(),
                import_job_id=job.id,
                attempt_number=number,
                attachment_id=attachment.id,
                availability="missing" if number == 1 else "available",
                archive_object_id=archive.id if number == 2 else None,
                source_member="member:/private/source.png",
                raw_json_pointer="reference:/private/source",
                observed_filename="source.png",
                observed_media_type="image/png",
                observed_size_bytes=1 if number == 2 else None,
                metadata_json={},
                resolution_status="missing" if number == 1 else "available",
                canonical_fragment_fingerprint=str(number) * 64,
                fragment_fingerprint_version="1",
                observed_at=now + timedelta(seconds=number),
            )
            for number in (1, 2)
        ]
        db.add_all(attempts)
        db.flush()

        service = EvidenceQueryService(db)
        observation_page_1 = service.list_conversation_observations(
            limit=1, source_account_id=account.id
        )
        assert observation_page_1.has_more is True
        initial_upper = observation_page_1.upper_bound
        initial_after = (
            observation_page_1.items[0]["committed_at"],
            observation_page_1.items[0]["conversation_observation_id"],
        )

        late_conversation = Conversation(
            id=uuid.uuid4(),
            source_account_id=account.id,
            external_conversation_id=f"evidence-late-{uuid.uuid4()}",
            title="late convenience title",
            metadata_json={},
        )
        db.add(late_conversation)
        db.flush()
        late_batch = ImportBatchCommit(
            id=uuid.uuid4(),
            import_job_id=job.id,
            conversation_id=late_conversation.id,
            sequence_no=3,
            batch_key=f"evidence-late-batch-{uuid.uuid4()}",
            external_conversation_id=late_conversation.external_conversation_id,
            parser_name="chatgpt-export",
            contract_version="1",
            report_delta={},
            counts_delta={},
            committed_at=now - timedelta(seconds=1),
        )
        db.add(late_batch)
        db.flush()
        late_observation = ConversationObservation(
            id=uuid.uuid4(),
            import_batch_commit_id=late_batch.id,
            conversation_id=late_conversation.id,
            archive_object_id=archive.id,
            source_member="late/conversations.json",
            source_conversation_index=2,
            raw_conversation_pointer="/late",
            current_node_status="absent",
            source_conversation_json={"title": "late historical"},
            create_time_parse_status="absent",
            update_time_parse_status="absent",
            time_provenance_json={},
            source_observation_fingerprint=_fingerprint("5"),
            canonical_fragment_fingerprint=_fingerprint("6"),
            fragment_fingerprint_version="1",
            observed_at=now + timedelta(seconds=3),
        )
        db.add(late_observation)
        db.flush()
        assert late_batch.committed_at < initial_after[0]

        observation_page_2 = service.list_conversation_observations(
            limit=1,
            source_account_id=account.id,
            upper_bound=initial_upper,
            after=initial_after,
        )
        assert observation_page_2.upper_bound == initial_upper
        assert observation_page_2.items[0]["conversation_observation_id"] == observations[1].id
        bounded_pass_ids = {
            observation_page_1.items[0]["conversation_observation_id"],
            observation_page_2.items[0]["conversation_observation_id"],
        }
        assert late_observation.id not in bounded_pass_ids

        fresh_rescan = service.list_conversation_observations(
            limit=10, source_account_id=account.id
        )
        fresh_ids = {
            item["conversation_observation_id"] for item in fresh_rescan.items
        }
        assert late_observation.id in fresh_ids
        deduplicated_ids = bounded_pass_ids | fresh_ids
        assert deduplicated_ids == {
            observations[0].id,
            observations[1].id,
            late_observation.id,
        }

        node_page_1 = service.list_observation_nodes(
            observation_id=observations[0].id, limit=1
        )
        assert node_page_1.has_more is True
        assert node_page_1.items[0]["declared_child_conversation_node_ids"] == [child.id]
        assert node_page_1.items[0]["message"]["attachments"] == [
            {
                "attachment_id": attachment.id,
                "filename": "source.png",
                "media_type": "image/png",
                "size_bytes": 1,
            }
        ]
        node_page_2 = service.list_observation_nodes(
            observation_id=observations[0].id,
            limit=1,
            after=(
                node_page_1.items[0]["observation_order"],
                node_page_1.items[0]["mapping_node_observation_id"],
            ),
        )
        assert node_page_2.items[0]["conversation_node_id"] == child.id

        attempt_page_1 = service.list_attachment_observations(
            attachment_id=attachment.id,
            conversation_observation_id=observations[0].id,
            limit=1,
        )
        assert attempt_page_1.has_more is True
        attempt_page_2 = service.list_attachment_observations(
            attachment_id=attachment.id,
            conversation_observation_id=observations[0].id,
            limit=1,
            upper_bound=attempt_page_1.upper_bound,
            after=(
                attempt_page_1.items[0]["attempt_number"],
                attempt_page_1.items[0]["observed_at"],
                attempt_page_1.items[0]["attachment_observation_id"],
            ),
        )
        assert attempt_page_2.items[0]["attempt_number"] == 2
        assert service.list_attachment_observations(
            attachment_id=attachment.id,
            conversation_observation_id=observations[1].id,
            limit=1,
        ) is None
        assert "member:/" not in str(attempt_page_1)
        assert "reference:" not in str(attempt_page_1)
    finally:
        db.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()
