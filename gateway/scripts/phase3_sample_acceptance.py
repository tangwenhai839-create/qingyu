"""Import or validate Phase 3 Sample #001 in an isolated acceptance environment.

This is an operator acceptance helper, not a runtime service entry point.  It
uses only Gateway evidence APIs and never connects to a database name unless
the caller explicitly configures DATABASE_URL.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import uuid
import zipfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.archive.manifest import render_manifest
from app.archive.service import ArchiveService
from app.db import engine
from app.importers.processor import ImportProcessor
from app.importers.query import EvidenceTreeQuery
from app.importers.real_chatgpt import (
    FRAGMENT_FINGERPRINT_VERSION,
    ChatGPTExportParser,
)
from app.importers.revision import compute_canonical_fragment_fingerprint
from app.importers.service import ImportService
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
    MessageRevision,
    SourceAccount,
)
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

SAMPLE_SHA256 = "04c54e6f87bd2113c5183ba7a2f24b2694c333e5eab6f42308f654063ee4b040"
EXPECTED = {
    "conversations": 15,
    "nodes": 944,
    "messages": 929,
    "null_nodes": 15,
    "roots": 15,
    "branch_parents": 3,
    "leaves": 18,
    "current_nodes": 15,
    "current_path_nodes": 941,
    "attachments": 9,
}
ALLOWED_DATABASES = {
    "qingyu_phase3_commit5_source",
    "qingyu_phase3_commit5_restore",
}


def _database_guard(session: Session) -> str:
    database = session.scalar(text("select current_database()"))
    if database not in ALLOWED_DATABASES:
        raise RuntimeError(f"refusing acceptance operation on database {database!r}")
    return database


def _sample_from_archive(root: Path) -> Path:
    record = None
    with Session(engine) as session:
        _database_guard(session)
        record = session.scalar(select(ArchiveObject).where(ArchiveObject.sha256 == SAMPLE_SHA256))
    if record is None:
        raise AssertionError("Sample #001 ArchiveObject is missing")
    return root / record.relative_path


def import_sample(sample: Path, root: Path) -> dict:
    if hashlib.sha256(sample.read_bytes()).hexdigest() != SAMPLE_SHA256:
        raise AssertionError("file is not approved Sample #001")
    with Session(engine, expire_on_commit=False) as session:
        database = _database_guard(session)
        archived = ArchiveService(root, session).archive_file(
            sample,
            original_filename=sample.name,
            source_platform="chatgpt",
            media_type="application/zip",
        )
        account = SourceAccount(
            platform="chatgpt",
            account_label=f"commit5-{uuid.uuid4()}",
            metadata_json={},
        )
        session.add(account)
        session.flush()
        token = uuid.uuid4()
        job = ImportJob(
            source_account_id=account.id,
            archive_object_id=archived.object_id,
            parser_name="pending-detection",
            parser_version="1",
            status="running",
            attempt_count=1,
            lease_token=token,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=10),
            report_json={},
        )
        session.add(job)
        session.commit()
        result = ImportProcessor(session, root).process(job, token)
        if result["imported_conversations"] != EXPECTED["conversations"]:
            raise AssertionError(result)
        return {"database": database, "job_id": str(job.id), "archive_object_id": str(archived.object_id)}


def validate_restored(root: Path) -> dict:
    sample = _sample_from_archive(root)
    if not sample.is_file() or hashlib.sha256(sample.read_bytes()).hexdigest() != SAMPLE_SHA256:
        raise AssertionError("restored raw Sample #001 failed SHA-256 verification")
    with zipfile.ZipFile(sample) as source:
        payload = json.loads(source.read("conversations.json"))
        dat_assets = {}
        for name in source.namelist():
            if not name.endswith(".dat"):
                continue
            stem = Path(name).stem
            if stem in dat_assets:
                raise AssertionError(f"ambiguous source asset: {stem}")
            content = source.read(name)
            dat_assets[stem] = {
                "member": name,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
    parsed = tuple(ChatGPTExportParser().parse(payload))

    with Session(engine) as session:
        database = _database_guard(session)
        job = session.scalar(
            select(ImportJob)
            .where(ImportJob.archive_object_id == select(ArchiveObject.id).where(ArchiveObject.sha256 == SAMPLE_SHA256).scalar_subquery())
            .order_by(ImportJob.created_at.desc())
        )
        if job is None or job.status != "completed":
            raise AssertionError("restored Sample #001 import job is not complete")
        account_id = job.source_account_id
        batches = list(session.scalars(
            select(ImportBatchCommit)
            .where(ImportBatchCommit.import_job_id == job.id)
            .order_by(ImportBatchCommit.sequence_no)
        ))
        counts = {
            "conversations": session.scalar(select(func.count()).select_from(Conversation).where(Conversation.source_account_id == account_id)),
            "nodes": session.scalar(select(func.count()).select_from(ConversationNode).join(Conversation).where(Conversation.source_account_id == account_id)),
            "messages": session.scalar(select(func.count()).select_from(Message).join(Conversation).where(Conversation.source_account_id == account_id)),
            "attachments": session.scalar(select(func.count()).select_from(Attachment).join(MessageRevision).join(Message).join(Conversation).where(Conversation.source_account_id == account_id)),
        }
        node_rows = list(session.scalars(
            select(MappingNodeObservation)
            .join(ConversationObservation)
            .join(ImportBatchCommit)
            .where(ImportBatchCommit.import_job_id == job.id)
        ))
        counts["null_nodes"] = sum(row.message_presence == "null" for row in node_rows)
        counts["roots"] = sum(row.parent_conversation_node_id is None for row in node_rows)
        query = EvidenceTreeQuery(session)
        import_service = ImportService(session)
        topology = Counter()
        reconstruction_hash = hashlib.sha256()
        linked_attachment_objects = set()
        descriptor_count = 0
        source_reference_count = 0
        asset_pointer_count = 0
        for batch, source_conversation in zip(batches, parsed, strict=True):
            restored = query.reconstruct(batch.id)
            reconstruction_hash.update(
                json.dumps(
                    restored,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            )
            if restored["source_conversation_json"] != source_conversation.source_conversation_json:
                raise AssertionError("conversation evidence mismatch after restore")
            conversation_observation = session.scalar(
                select(ConversationObservation).where(
                    ConversationObservation.import_batch_commit_id == batch.id
                )
            )
            projection_rows = session.execute(
                select(MappingNodeObservation, ConversationNode, Message, MessageRevision)
                .join(
                    ConversationNode,
                    ConversationNode.id == MappingNodeObservation.conversation_node_id,
                )
                .outerjoin(Message, Message.id == MappingNodeObservation.message_id)
                .outerjoin(
                    MessageRevision,
                    MessageRevision.id == MappingNodeObservation.message_revision_id,
                )
                .where(
                    MappingNodeObservation.conversation_observation_id
                    == conversation_observation.id
                )
                .order_by(MappingNodeObservation.observation_order)
            ).all()
            resolved_nodes = {node.external_node_id: node for _row, node, _message, _revision in projection_rows}
            resolved_messages = {
                node.external_node_id: message
                for _row, node, message, _revision in projection_rows
                if message is not None
            }
            revisions = {
                node.external_node_id: revision
                for _row, node, _message, revision in projection_rows
                if revision is not None
            }
            # This is the same exhaustive comparison used by fail-closed replay:
            # conversation timestamps/provenance and fingerprints, ordered node
            # topology, revisions, message observations, raw pointers and all
            # fingerprint versions are checked against the freshly parsed source.
            import_service._validate_real_export_snapshot(
                batch=batch,
                parsed=source_conversation,
                archive_object_id=job.archive_object_id,
                source_member="conversations.json",
                resolved_nodes=resolved_nodes,
                resolved_messages=resolved_messages,
                revisions=revisions,
            )
            source_nodes = list(source_conversation.nodes)
            restored_nodes = restored["nodes"]
            if len(source_nodes) != len(restored_nodes):
                raise AssertionError("node count mismatch after restore")
            by_id = {node["node_id"]: node for node in restored_nodes}
            children = Counter(node["parent_node_id"] for node in restored_nodes if node["parent_node_id"] is not None)
            topology["branch_parents"] += sum(value > 1 for value in children.values())
            topology["leaves"] += sum(node_id not in children for node_id in by_id)
            topology["current_nodes"] += int(restored["current_node_status"] == "resolved")
            cursor = restored["current_node"]
            while cursor is not None:
                topology["current_path_nodes"] += 1
                cursor = by_id[cursor]["parent_node_id"]
            for expected_node, actual_node in zip(source_nodes, restored_nodes, strict=True):
                if (
                    actual_node["node_id"] != expected_node.node_id
                    or actual_node["parent_node_id"] != expected_node.parent_node_id
                    or actual_node["declared_children"] != (
                        list(expected_node.declared_children)
                        if expected_node.declared_children is not None else None
                    )
                    or actual_node["message_presence"] != expected_node.message_presence
                    or actual_node["source_node_json"] != expected_node.source_node_json
                ):
                    raise AssertionError("node evidence mismatch after restore")
                if expected_node.message is None:
                    continue
                expected_message = expected_node.message
                revision = revisions[expected_node.node_id]
                for declared in expected_message.attachments:
                    descriptor_count += int(bool(declared.descriptor_json))
                    source_reference_count += len(declared.source_references)
                    asset_pointer_count += sum(
                        reference.get("kind") == "asset_pointer"
                        for reference in declared.source_references
                    )
                    attachment = session.scalar(
                        select(Attachment).where(
                            Attachment.message_revision_id == revision.id,
                            Attachment.stable_attachment_key == declared.stable_attachment_key,
                        )
                    )
                    if attachment is None:
                        raise AssertionError("source attachment is missing from normalized evidence")
                    stable_projection = (
                        attachment.external_attachment_id,
                        attachment.original_reference,
                        attachment.filename,
                        attachment.media_type,
                        attachment.size_bytes,
                        attachment.descriptor_json,
                    )
                    expected_projection = (
                        declared.external_attachment_id,
                        declared.original_reference,
                        declared.filename,
                        declared.media_type,
                        declared.size_bytes,
                        declared.descriptor_json,
                    )
                    if stable_projection != expected_projection:
                        raise AssertionError("attachment descriptor projection mismatch")
                    attachment_observation = session.scalar(
                        select(AttachmentObservation)
                        .where(
                            AttachmentObservation.import_job_id == job.id,
                            AttachmentObservation.attachment_id == attachment.id,
                        )
                        .order_by(AttachmentObservation.attempt_number)
                    )
                    if attachment_observation is None:
                        raise AssertionError("attachment observation is missing")
                    expected_fragment = compute_canonical_fragment_fingerprint(
                        declared.source_fragment_json,
                        declared.raw_json_pointer,
                    )
                    if (
                        attachment_observation.availability != "available"
                        or attachment_observation.resolution_status != "resolved"
                        or attachment_observation.source_member != "conversations.json"
                        or attachment_observation.raw_json_pointer != declared.raw_json_pointer
                        or attachment_observation.source_references_json != list(declared.source_references)
                        or attachment_observation.canonical_fragment_fingerprint != expected_fragment
                        or attachment_observation.fragment_fingerprint_version != FRAGMENT_FINGERPRINT_VERSION
                        or attachment_observation.archive_object_id is None
                    ):
                        raise AssertionError("attachment observation evidence mismatch")
                    source_asset = dat_assets.get(declared.external_attachment_id)
                    linked_object = session.get(
                        ArchiveObject,
                        attachment_observation.archive_object_id,
                    )
                    if source_asset is None or linked_object is None:
                        raise AssertionError("attachment source-byte chain is incomplete")
                    if (
                        attachment_observation.attachment_member_path != source_asset["member"]
                        or attachment_observation.observed_filename != Path(source_asset["member"]).name
                        or attachment_observation.observed_size_bytes != source_asset["size_bytes"]
                        or linked_object.size_bytes != source_asset["size_bytes"]
                        or linked_object.sha256 != source_asset["sha256"]
                        or linked_object.media_type != attachment_observation.observed_media_type
                    ):
                        raise AssertionError("attachment source ZIP bytes do not match linked ArchiveObject")
                    linked_attachment_objects.add(linked_object.id)
        counts.update(topology)
        if counts != EXPECTED:
            raise AssertionError({"expected": EXPECTED, "actual": counts})

        observations = list(session.scalars(
            select(AttachmentObservation).where(AttachmentObservation.import_job_id == job.id)
        ))
        if len(observations) != 9 or any(
            item.availability != "available" or item.archive_object_id is None
            for item in observations
        ):
            raise AssertionError("attachment evidence is incomplete after restore")
        logical_ids = {
            item.external_attachment_id
            for item in session.scalars(select(Attachment).join(MessageRevision).join(Message).join(Conversation).where(Conversation.source_account_id == account_id))
        }
        if None in logical_ids or len(set(dat_assets) - logical_ids) != 3:
            raise AssertionError("unreferenced raw blobs were promoted into attachments")
        if (
            len(linked_attachment_objects) != 9
            or descriptor_count != 5
            or source_reference_count != 13
            or asset_pointer_count != 8
        ):
            raise AssertionError("attachment descriptor/reference acceptance matrix mismatch")
        archive_records = list(session.scalars(select(ArchiveObject)))
        if len(archive_records) != 10:
            raise AssertionError("expected one raw ZIP plus nine referenced attachment objects")
        expected_object_paths = {record.relative_path for record in archive_records}
        object_root = root / "archive" / "objects"
        actual_object_paths = {
            path.relative_to(root).as_posix()
            for path in object_root.rglob("*")
            if path.is_file()
        }
        orphan_objects = sorted(actual_object_paths - expected_object_paths)
        if expected_object_paths - actual_object_paths:
            raise AssertionError("database references a missing archive object")
        for record in archive_records:
            path = root / record.relative_path
            if not path.is_file() or path.stat().st_size != record.size_bytes:
                raise AssertionError(f"archive object missing: {record.id}")
            if hashlib.sha256(path.read_bytes()).hexdigest() != record.sha256:
                raise AssertionError(f"archive digest mismatch: {record.id}")
            manifest = root / "manifests" / f"{record.sha256}.json"
            if not manifest.is_file():
                raise AssertionError(f"manifest missing: {record.id}")
            manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
            if manifest_payload != render_manifest(record):
                raise AssertionError(f"manifest mismatch for {record.id}")
        return {
            "database": database,
            "job_id": str(job.id),
            **counts,
            "referenced_bytes": 9,
            "raw_only_blobs": 3,
            "evidence_digest": reconstruction_hash.hexdigest(),
            "archive_objects": len(archive_records),
            "orphan_objects": orphan_objects,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("import", "validate"))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--sample", type=Path)
    args = parser.parse_args()
    if args.mode == "import":
        if args.sample is None:
            parser.error("--sample is required for import")
        result = import_sample(args.sample, args.root)
    else:
        result = validate_restored(args.root)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
