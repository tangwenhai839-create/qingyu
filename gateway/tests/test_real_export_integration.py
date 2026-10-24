import hashlib
import os
import shutil
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.db import engine
from app.importers.processor import ImportProcessor
from app.importers.query import EvidenceTreeQuery, ImportedTreeQuery
from app.importers.real_chatgpt import ChatGPTExportParser
from app.importers.service import BatchReplayIdentityError, ImportService
from app.importers.worker import ImportWorker
from app.models import (
    ArchiveObject,
    ArchiveObservation,
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
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

EXPECTED_SAMPLE_SHA256 = "04c54e6f87bd2113c5183ba7a2f24b2694c333e5eab6f42308f654063ee4b040"


@pytest.fixture(autouse=True)
def clean_real_export_integration_state():
    models = (
        MappingNodeObservation,
        ConversationObservation,
        AttachmentObservation,
        Attachment,
        MessageObservation,
        MessageRevision,
        Message,
        ConversationNode,
        ImportBatchCommit,
        ImportJob,
        Conversation,
        ArchiveObservation,
        SourceAccount,
        ArchiveObject,
    )
    with Session(engine) as db:
        for model in models:
            db.execute(delete(model))
        db.commit()
    yield
    with Session(engine) as db:
        for model in models:
            db.execute(delete(model))
        db.commit()
    root = Path("<repo>/.phase3-real-export-test")
    if root.exists():
        shutil.rmtree(root)


def _sample_path() -> Path:
    configured = os.getenv("QINGYU_REAL_EXPORT_SAMPLE")
    if configured:
        return Path(configured)
    matches = list(Path("<home>/Desktop").glob(
        "254267e6bfa8fc2484b4c2dac7d7f744fa771fbb4aa5fff49a12f7805ea848ce-*.zip"
    ))
    if not matches:
        pytest.skip("Real Export Sample #001 is not available on this host")
    return matches[0]


def test_sample_001_normalized_evidence_reconstructs_complete_tree():
    sample = _sample_path()
    content_hash = hashlib.sha256(sample.read_bytes()).hexdigest()
    assert content_hash == EXPECTED_SAMPLE_SHA256
    import json
    import zipfile
    with zipfile.ZipFile(sample) as source:
        source_payload = json.loads(source.read("conversations.json"))
    parsed_source = tuple(ChatGPTExportParser().parse(source_payload))
    # Keep the extraction prefix short enough for Win32 paths in the real ZIP.
    root = Path("<repo>/.phase3-real-export-test")
    target = root / "archive" / "objects" / content_hash[:2] / content_hash
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(sample, target)
    account_label = f"real-export-{uuid.uuid4()}"

    with Session(engine, expire_on_commit=False) as db:
        archive = db.scalar(select(ArchiveObject).where(
            ArchiveObject.sha256 == content_hash,
            ArchiveObject.size_bytes == sample.stat().st_size,
        ))
        if archive is None:
            archive = ArchiveObject(
                sha256=content_hash,
                size_bytes=sample.stat().st_size,
                relative_path=target.relative_to(root).as_posix(),
                media_type="application/zip",
                source_platform="chatgpt",
                integrity_status="verified",
                metadata_json={},
            )
        account = SourceAccount(platform="chatgpt", account_label=account_label, metadata_json={})
        db.add_all([archive, account])
        db.flush()
        job = ImportJob(
            source_account_id=account.id,
            archive_object_id=archive.id,
            parser_name="pending-detection",
            parser_version="1",
            status="running",
            attempt_count=1,
            lease_token=uuid.uuid4(),
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
            report_json={},
        )
        db.add(job)
        db.commit()

        result = ImportProcessor(db, root).process(job, job.lease_token)
        assert result["imported_conversations"] == 15
        assert result["imported_messages"] == 929
        db.expire_all()
        assert db.get(ImportJob, job.id).parser_name == "chatgpt-export"
        assert db.get(ImportJob, job.id).parser_version == "1"

        conversations = list(db.scalars(select(Conversation).where(Conversation.source_account_id == account.id)))
        batches = list(db.scalars(select(ImportBatchCommit).where(ImportBatchCommit.import_job_id == job.id)))
        observations = list(db.scalars(
            select(ConversationObservation).join(ImportBatchCommit).where(ImportBatchCommit.import_job_id == job.id)
        ))
        node_rows = list(db.scalars(
            select(MappingNodeObservation)
            .join(ConversationObservation)
            .join(ImportBatchCommit)
            .where(ImportBatchCommit.import_job_id == job.id)
        ))
        assert len(conversations) == len(batches) == len(observations) == 15
        assert db.scalar(select(__import__("sqlalchemy").func.count()).select_from(ConversationNode).join(Conversation).where(Conversation.source_account_id == account.id)) == 944
        assert len(node_rows) == 944
        assert Counter(row.message_presence for row in node_rows) == {"object": 929, "null": 15}
        assert sum(row.parent_conversation_node_id is None for row in node_rows) == 15
        assert db.scalar(select(__import__("sqlalchemy").func.count()).select_from(Message).join(Conversation).where(Conversation.source_account_id == account.id)) == 929
        assert db.scalar(select(__import__("sqlalchemy").func.count()).select_from(MessageRevision).join(Message).join(Conversation).where(Conversation.source_account_id == account.id)) == 929

        total_current_path = 0
        branch_parents = 0
        leaves = 0
        query = EvidenceTreeQuery(db)
        for batch in sorted(batches, key=lambda item: item.sequence_no):
            reconstructed = query.reconstruct(batch.id)
            assert reconstructed is not None
            expected_conversation = parsed_source[batch.sequence_no - 1]
            assert reconstructed["source_conversation_index"] == batch.sequence_no - 1
            assert reconstructed["source_conversation_json"] == expected_conversation.source_conversation_json
            assert reconstructed["current_node"] == expected_conversation.current_node_id
            nodes = reconstructed["nodes"]
            assert len(nodes) == len(expected_conversation.nodes)
            for actual_node, expected_node in zip(nodes, expected_conversation.nodes, strict=True):
                assert actual_node["node_id"] == expected_node.node_id
                assert actual_node["parent_node_id"] == expected_node.parent_node_id
                assert actual_node["declared_children"] == (list(expected_node.declared_children) if expected_node.declared_children is not None else None)
                assert actual_node["message_presence"] == expected_node.message_presence
                assert actual_node["source_node_json"] == expected_node.source_node_json
                assert actual_node["raw_mapping_pointer"] == expected_node.raw_json_pointer
                if expected_node.message is None:
                    assert actual_node["message_id"] is None
                    assert actual_node["semantic_evidence_json"] is None
                    assert actual_node["message_observation_json"] is None
                else:
                    assert actual_node["message_id"] == expected_node.message.external_message_id
                    assert actual_node["semantic_evidence_json"] == expected_node.message.semantic_evidence_json
                    assert actual_node["message_observation_json"] == expected_node.message.observation_evidence_json
                    assert actual_node["raw_message_pointer"] == expected_node.message.raw_json_pointer
            by_id = {node["node_id"]: node for node in nodes}
            children = Counter(node["parent_node_id"] for node in nodes if node["parent_node_id"] is not None)
            branch_parents += sum(count > 1 for count in children.values())
            leaves += sum(node["node_id"] not in children for node in nodes)
            current = reconstructed["current_node"]
            assert reconstructed["current_node_status"] == "resolved"
            path = []
            while current is not None:
                assert current in by_id
                path.append(current)
                current = by_id[current]["parent_node_id"]
            total_current_path += len(path)
        assert branch_parents == 3
        assert leaves == 18
        assert total_current_path == 941

        attachments = list(db.scalars(select(Attachment).join(MessageRevision).join(Message).join(Conversation).where(Conversation.source_account_id == account.id)))
        attachment_observations = list(db.scalars(select(AttachmentObservation).where(AttachmentObservation.import_job_id == job.id)))
        assert len(attachments) == len(attachment_observations) == 9
        assert all(item.availability == "available" and item.resolution_status == "resolved" and item.archive_object_id for item in attachment_observations)

        selected_attachment = attachments[0]
        selected_observation = next(
            item for item in attachment_observations if item.attachment_id == selected_attachment.id
        )

        # A clean retry revalidates the earliest committed attachment evidence,
        # succeeds, and does not append attempt-scoped replacement history.
        clean_token = uuid.uuid4()
        persisted_job = db.get(ImportJob, job.id)
        persisted_job.status = "running"
        persisted_job.lease_token = clean_token
        persisted_job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        db.commit()
        assert ImportWorker(db).mark_failed_or_partial(
            job.id, clean_token, "forced_replay", {"errors": []}, "forced_replay"
        ) == "partial"
        assert ImportWorker(db).retry(job.id, "chatgpt-export", "1")
        db.expire_all()
        clean_replay_job = db.get(ImportJob, job.id)
        clean_replay_job.status = "running"
        clean_replay_job.attempt_count = 2
        clean_replay_job.lease_token = clean_token
        clean_replay_job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        db.commit()
        clean_result = ImportProcessor(db, root).process(clean_replay_job, clean_token)
        assert clean_result["attachments"]["available"] == 9
        assert db.scalar(
            select(__import__("sqlalchemy").func.count())
            .select_from(AttachmentObservation)
            .where(AttachmentObservation.import_job_id == job.id)
        ) == 9

        db.add(AttachmentObservation(
            import_job_id=job.id,
            attempt_number=3,
            attachment_id=selected_attachment.id,
            availability="missing",
            resolution_status="missing",
            source_member=selected_observation.source_member,
            raw_json_pointer=selected_observation.raw_json_pointer,
            source_references_json=selected_observation.source_references_json,
            canonical_fragment_fingerprint=selected_observation.canonical_fragment_fingerprint,
            fragment_fingerprint_version=selected_observation.fragment_fingerprint_version,
        ))
        db.commit()
        selected_batch_id = db.scalar(
            select(ImportBatchCommit.id)
            .join(ConversationObservation)
            .join(MappingNodeObservation)
            .join(Attachment, Attachment.message_revision_id == MappingNodeObservation.message_revision_id)
            .where(Attachment.id == selected_attachment.id)
        )
        reconstructed = EvidenceTreeQuery(db).reconstruct(selected_batch_id)
        matching = [
            item
            for node in reconstructed["nodes"]
            for item in node["attachments"]
            if item["stable_attachment_key"] == selected_attachment.stable_attachment_key
        ]
        assert len(matching) == 1
        # A committed batch reconstructs from its earliest durable attachment
        # observation.  Later retry observations are operational history and
        # must not rewrite the already-committed evidence tree.
        assert matching[0]["availability"] == "available"

        # Replaying the durable batch must validate against that same frozen
        # observation.  If it has drifted, fail closed without appending yet
        # another observation that would silently redefine history.
        selected_observation.availability = "missing"
        selected_observation.resolution_status = "missing"
        db.commit()
        replay_token = uuid.uuid4()
        db.expire_all()
        persisted_job = db.get(ImportJob, job.id)
        persisted_job.status = "running"
        persisted_job.lease_token = replay_token
        persisted_job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        db.commit()
        assert ImportWorker(db).mark_failed_or_partial(
            job.id,
            replay_token,
            "forced_replay",
            {"errors": []},
            "forced_replay",
        ) == "partial"
        assert ImportWorker(db).retry(job.id, "chatgpt-export", "1")
        db.expire_all()
        replay_job = db.get(ImportJob, job.id)
        replay_job.status = "running"
        replay_job.attempt_count = 3
        replay_job.lease_token = replay_token
        replay_job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        db.commit()
        observation_count = db.scalar(
            select(__import__("sqlalchemy").func.count())
            .select_from(AttachmentObservation)
            .where(AttachmentObservation.import_job_id == job.id)
        )
        with pytest.raises(BatchReplayIdentityError, match="attachment_observation_replay_mismatch"):
            ImportProcessor(db, root).process(replay_job, replay_token)
        assert db.scalar(
            select(__import__("sqlalchemy").func.count())
            .select_from(AttachmentObservation)
            .where(AttachmentObservation.import_job_id == job.id)
        ) == observation_count

        # If every durable observation for an attachment is missing, replay
        # must fail before it can manufacture a replacement observation.
        db.execute(
            delete(AttachmentObservation).where(
                AttachmentObservation.import_job_id == job.id,
                AttachmentObservation.attachment_id == selected_attachment.id,
            )
        )
        db.commit()
        db.expire_all()
        missing_replay_job = db.get(ImportJob, job.id)
        missing_token = uuid.uuid4()
        missing_replay_job.status = "running"
        missing_replay_job.attempt_count = 4
        missing_replay_job.lease_token = missing_token
        missing_replay_job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        db.commit()
        missing_count = db.scalar(
            select(__import__("sqlalchemy").func.count())
            .select_from(AttachmentObservation)
            .where(AttachmentObservation.import_job_id == job.id)
        )
        with pytest.raises(BatchReplayIdentityError, match="attachment_observation_replay_missing"):
            ImportProcessor(db, root).process(missing_replay_job, missing_token)
        assert db.scalar(
            select(__import__("sqlalchemy").func.count())
            .select_from(AttachmentObservation)
            .where(AttachmentObservation.import_job_id == job.id)
        ) == missing_count


def test_real_export_partial_retry_replays_snapshot_without_repair():
    payload = [
        {
            "id": f"retry-{uuid.uuid4()}-{index}",
            "title": "retry",
            "current_node": "message-node",
            "mapping": {
                "root": {"id": "root", "parent": None, "message": None},
                "message-node": {
                    "id": "message-node",
                    "parent": "root",
                    "message": {
                        "id": f"message-{index}",
                        "author": {"role": "user", "name": None},
                        "create_time": None,
                        "content": {"content_type": "text", "parts": ["retry evidence"]},
                        "metadata": {},
                    },
                },
            },
        }
        for index in range(2)
    ]
    parser = ChatGPTExportParser()
    parsed = tuple(parser.parse(payload))
    token = uuid.uuid4()
    with Session(engine, expire_on_commit=False) as db:
        archive = ArchiveObject(sha256=uuid.uuid4().hex * 2, size_bytes=1, relative_path="unused", media_type="application/zip", integrity_status="verified", metadata_json={})
        account = SourceAccount(platform="chatgpt", account_label=f"retry-{uuid.uuid4()}", metadata_json={})
        db.add_all([archive, account]); db.flush()
        job = ImportJob(source_account_id=account.id, archive_object_id=archive.id, parser_name="pending-detection", parser_version="1", status="running", attempt_count=1, max_attempts=3, lease_token=token, lease_expires_at=datetime.now(UTC)+timedelta(minutes=5), report_json={})
        db.add(job); db.commit()

        calls = 0
        def crash_before_second_conversation():
            nonlocal calls
            calls += 1
            if calls == 3:
                raise RuntimeError("crash-equivalent")

        with pytest.raises(RuntimeError, match="crash-equivalent"):
            ImportService(db).import_fixture(payload, archive.id, job=job, lease_token=token, source_member="conversations.json", parser=parser, parsed_conversations=parsed, health_check=crash_before_second_conversation)
        assert db.scalar(select(__import__("sqlalchemy").func.count()).select_from(ImportBatchCommit).where(ImportBatchCommit.import_job_id == job.id)) == 1
        assert ImportWorker(db).mark_failed_or_partial(job.id, token, "processor_error", {"errors": []}, "processor_error") == "partial"
        assert ImportWorker(db).retry(job.id, "chatgpt-export", "1")
        db.expire_all()
        retried = db.get(ImportJob, job.id)
        new_token = uuid.uuid4()
        retried.status = "running"; retried.attempt_count = 2; retried.lease_token = new_token; retried.lease_expires_at = datetime.now(UTC)+timedelta(minutes=5)
        db.commit()
        outcome = ImportService(db).import_fixture(payload, archive.id, job=retried, lease_token=new_token, source_member="conversations.json", parser=parser, parsed_conversations=parsed)
        assert outcome.counts["discovered_count"] == 2
        assert db.scalar(select(__import__("sqlalchemy").func.count()).select_from(ImportBatchCommit).where(ImportBatchCommit.import_job_id == job.id)) == 2
        assert db.scalar(select(__import__("sqlalchemy").func.count()).select_from(ConversationObservation).join(ImportBatchCommit).where(ImportBatchCommit.import_job_id == job.id)) == 2
        assert db.scalar(select(__import__("sqlalchemy").func.count()).select_from(MappingNodeObservation).join(ConversationObservation).join(ImportBatchCommit).where(ImportBatchCommit.import_job_id == job.id)) == 4
        with pytest.raises(ValueError, match="real_export_requires_evidence_tree_query"):
            ImportedTreeQuery(db).get_tree(retried.source_account_id, job.id)

        first_observation = db.scalar(
            select(ConversationObservation)
            .join(ImportBatchCommit)
            .where(ImportBatchCommit.import_job_id == job.id)
            .order_by(ImportBatchCommit.sequence_no)
        )
        original_envelope = first_observation.source_conversation_json
        first_observation.source_conversation_json = {"tampered": True}
        db.commit()
        with pytest.raises(BatchReplayIdentityError, match="conversation_observation_mismatch"):
            ImportService(db).import_fixture(payload, archive.id, job=retried, lease_token=new_token, source_member="conversations.json", parser=parser, parsed_conversations=parsed)

        first_observation.source_conversation_json = original_envelope
        first_revision_id = db.scalar(
            select(MappingNodeObservation.message_revision_id)
            .where(
                MappingNodeObservation.conversation_observation_id == first_observation.id,
                MappingNodeObservation.message_revision_id.is_not(None),
            )
        )
        message_observation = db.scalar(
            select(MessageObservation)
            .where(
                MessageObservation.import_job_id == job.id,
                MessageObservation.message_revision_id == first_revision_id,
            )
        )
        revision = db.get(MessageRevision, first_revision_id)
        original_semantic_evidence = revision.semantic_evidence_json
        revision.semantic_evidence_json = {"tampered": True}
        db.commit()
        with pytest.raises(BatchReplayIdentityError, match="revision_evidence_mismatch"):
            ImportService(db).import_fixture(payload, archive.id, job=retried, lease_token=new_token, source_member="conversations.json", parser=parser, parsed_conversations=parsed)

        revision.semantic_evidence_json = original_semantic_evidence
        db.delete(message_observation)
        db.commit()
        with pytest.raises(BatchReplayIdentityError, match="message_observation_mismatch"):
            ImportService(db).import_fixture(payload, archive.id, job=retried, lease_token=new_token, source_member="conversations.json", parser=parser, parsed_conversations=parsed)
