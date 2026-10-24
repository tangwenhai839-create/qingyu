import hashlib
import json
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.archive.service import ArchiveService
from app.db import engine
from app.importers.processor import ImportProcessor
from app.importers.worker import ImportWorker, LeaseLost
from app.models import (
    ArchiveObject,
    ArchiveObservation,
    Attachment,
    AttachmentObservation,
    Conversation,
    ImportBatchCommit,
    ImportJob,
    Message,
    MessageObservation,
    MessageRevision,
    SourceAccount,
)
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session


def _clear(db):
    for model in (
        AttachmentObservation,
        Attachment,
        MessageObservation,
        ImportBatchCommit,
        MessageRevision,
        Message,
        Conversation,
        ArchiveObservation,
        ImportJob,
        SourceAccount,
        ArchiveObject,
    ):
        db.execute(delete(model))
    db.commit()


@pytest.fixture()
def clean_db():
    with Session(engine) as db:
        _clear(db)
        try:
            yield db
        finally:
            db.rollback()
            _clear(db)


def _payload(attachments):
    return {
        "schema_marker": "qingyu-fixture-v1",
        "conversations": [{
            "id": "attachment-conversation",
            "title": "attachments",
            "current_node": "message-1",
            "mapping": {"message-1": {
                "id": "message-1",
                "parent": None,
                "children": [],
                "metadata": {},
                "message": {
                    "id": "message-1",
                    "author": {"role": "user", "metadata": {}},
                    "content": {"content_type": "text", "parts": ["hello"], "metadata": {}},
                    "metadata": {},
                    "attachments": attachments,
                },
            }},
            "metadata": {},
        }],
    }


def _input_archive(root: Path, payload, members=None):
    source = root.parent / f"input-{uuid.uuid4()}.zip"
    with zipfile.ZipFile(source, "w") as bundle:
        bundle.writestr("fixture.json", json.dumps(payload))
        for name, content in (members or {}).items():
            bundle.writestr(name, content)
    content = source.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    target = root / "archive" / "objects" / digest[:2] / digest
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return digest, len(content), target.relative_to(root).as_posix()


def _claim(db, root, payload, members=None, account=None, archive=None):
    if account is None:
        account = SourceAccount(platform="fixture", account_label=str(uuid.uuid4()), metadata_json={})
        db.add(account)
    if archive is None:
        digest, size, relative = _input_archive(root, payload, members)
        archive = ArchiveObject(
            sha256=digest,
            size_bytes=size,
            relative_path=relative,
            media_type="application/zip",
            integrity_status="verified",
            metadata_json={},
        )
        db.add(archive)
    db.flush()
    job = ImportJob(
        source_account_id=account.id,
        archive_object_id=archive.id,
        parser_name="generic-tree-fixture",
        parser_version="0.1",
        status="pending",
        report_json={},
    )
    db.add(job)
    db.commit()
    return ImportWorker(db).claim_next(60), account, archive


def test_available_missing_and_referenced_only_attachments_are_observed(clean_db, tmp_path):
    db = clean_db
    root = tmp_path / "data"
    payload = _payload([
        {"id": "available", "member_path": "files/available.bin", "filename": "declared/path.bin"},
        {"id": "missing", "member_path": "files/missing.bin", "filename": "missing.bin"},
        {"id": "reference", "reference": "https://example.invalid/file"},
    ])
    claim, _account, input_archive = _claim(
        db, root, payload, {"files/available.bin": b"attachment bytes"}
    )

    report = ImportProcessor(db, root).process(claim, claim.lease_token)

    assert report["attachments"] == {
        "available": 1,
        "missing": 1,
        "referenced_only": 1,
        "failed": 0,
    }
    observations = list(db.scalars(select(AttachmentObservation).order_by(AttachmentObservation.availability)))
    assert {item.availability for item in observations} == {"available", "missing", "referenced_only"}
    available = next(item for item in observations if item.availability == "available")
    declared = db.get(Attachment, available.attachment_id)
    assert declared.filename == "declared/path.bin"
    assert available.observed_filename == "available.bin"
    assert available.import_job_id == claim.id and available.attempt_number == 1
    assert available.archive_object_id != input_archive.id
    assert available.source_member == "fixture.json"
    assert available.attachment_member_path == "files/available.bin"
    assert available.raw_json_pointer.endswith("/attachments/0")
    archived = db.get(ArchiveObject, available.archive_object_id)
    assert (root / archived.relative_path).read_bytes() == b"attachment bytes"
    assert (root / "manifests" / f"{archived.sha256}.json").exists()


def test_attachment_member_matching_is_exact_case(clean_db, tmp_path):
    db = clean_db
    root = tmp_path / "data"
    payload = _payload([{"id": "case", "member_path": "files/case.bin"}])
    claim, _account, _archive = _claim(db, root, payload, {"Files/Case.bin": b"wrong case"})

    report = ImportProcessor(db, root).process(claim, claim.lease_token)

    assert report["attachments"]["missing"] == 1
    observation = db.query(AttachmentObservation).one()
    assert observation.availability == "missing" and observation.archive_object_id is None


@pytest.mark.parametrize(
    ("declared", "error_code"),
    [
        ({"size_bytes": 999}, "attachment_size_mismatch"),
        ({"media_type": "image/png"}, "attachment_media_type_mismatch"),
    ],
)
def test_declared_attachment_integrity_mismatch_is_failed(clean_db, tmp_path, declared, error_code):
    db = clean_db
    root = tmp_path / "data"
    descriptor = {"id": error_code, "member_path": "files/item.bin", **declared}
    claim, _account, _archive = _claim(db, root, _payload([descriptor]), {"files/item.bin": b"actual"})

    report = ImportProcessor(db, root).process(claim, claim.lease_token)

    observation = db.query(AttachmentObservation).one()
    assert report["attachments"]["failed"] == 1
    assert observation.availability == "failed"
    assert observation.archive_object_id is None
    assert observation.metadata_json == {"error_code": error_code}
    assert observation.observed_filename == "item.bin"
    assert observation.observed_size_bytes == len(b"actual")


def test_declared_media_type_cannot_be_verified_by_filename_extension(clean_db, tmp_path):
    db = clean_db
    root = tmp_path / "data"
    descriptor = {"id": "forged", "member_path": "files/forged.png", "media_type": "image/png"}
    claim, _account, _archive = _claim(db, root, _payload([descriptor]), {"files/forged.png": b"not a png"})

    report = ImportProcessor(db, root).process(claim, claim.lease_token)

    observation = db.query(AttachmentObservation).one()
    assert report["attachments"]["failed"] == 1
    assert observation.availability == "failed"
    assert observation.observed_media_type == "application/octet-stream"
    assert observation.archive_object_id is None


def test_persist_failure_rolls_back_then_records_failed_observation(clean_db, tmp_path, monkeypatch):
    db = clean_db
    root = tmp_path / "data"
    payload = _payload([{"id": "persist-failure", "member_path": "files/item.bin"}])
    claim, _account, _archive = _claim(db, root, payload, {"files/item.bin": b"prepared orphan"})

    def fail_persist(*args, **kwargs):
        raise OSError("transient persist failure")

    monkeypatch.setattr(ArchiveService, "persist_prepared", fail_persist)
    report = ImportProcessor(db, root).process(claim, claim.lease_token)

    assert report["attachments"]["failed"] == 1
    observation = db.query(AttachmentObservation).one()
    assert observation.availability == "failed"
    assert observation.archive_object_id is None
    assert observation.metadata_json == {"error_code": "attachment_archive_failed"}
    assert db.query(ArchiveObservation).count() == 0
    assert db.query(ImportBatchCommit).count() == 1
    persisted_job = db.get(ImportJob, claim.id)
    assert persisted_job.status == "partial"
    assert persisted_job.checkpoint_sequence == 0


def test_failed_attachment_stops_later_batches_from_advancing_checkpoint(clean_db, tmp_path, monkeypatch):
    db = clean_db
    root = tmp_path / "data"
    payload = _payload([{"id": "first-fails", "member_path": "files/first.bin"}])
    second = json.loads(json.dumps(payload["conversations"][0]))
    second["id"] = "second-conversation"
    second["mapping"]["message-1"]["message"]["attachments"] = []
    payload["conversations"].append(second)
    claim, _account, _archive = _claim(db, root, payload, {"files/first.bin": b"first"})
    monkeypatch.setattr(ArchiveService, "prepare", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("fail")))

    report = ImportProcessor(db, root).process(claim, claim.lease_token)

    current = db.get(ImportJob, claim.id)
    assert report["attachments"]["failed"] == 1
    assert current.checkpoint_sequence == 0
    assert db.query(ImportBatchCommit).count() == 1
    assert db.query(Conversation).filter_by(external_conversation_id="second-conversation").count() == 0


def test_different_jobs_keep_distinct_attachment_observations_and_stable_identity(clean_db, tmp_path):
    db = clean_db
    root = tmp_path / "data"
    payload = _payload([{"id": "shared", "member_path": "files/shared.bin"}])
    claim_a, account, archive = _claim(db, root, payload, {"files/shared.bin": b"same"})
    ImportProcessor(db, root).process(claim_a, claim_a.lease_token)
    claim_b, _account, _archive = _claim(db, root, payload, account=account, archive=archive)

    report = ImportProcessor(db, root).process(claim_b, claim_b.lease_token)

    assert report["attachments"]["available"] == 1
    assert db.query(Attachment).count() == 1
    observations = db.query(AttachmentObservation).all()
    assert len(observations) == 2
    assert {item.import_job_id for item in observations} == {claim_a.id, claim_b.id}


def test_same_job_different_attempts_keep_failed_then_available_and_reuse_bytes(clean_db, tmp_path, monkeypatch):
    db = clean_db
    root = tmp_path / "data"
    payload = _payload([{"id": "retry", "member_path": "files/retry.bin"}])
    claim_a, _account, _archive = _claim(db, root, payload, {"files/retry.bin": b"retry bytes"})
    original_prepare = ArchiveService.prepare

    def fail_prepare(*args, **kwargs):
        raise OSError("transient failure")

    monkeypatch.setattr(ArchiveService, "prepare", fail_prepare)
    first_report = ImportProcessor(db, root).process(claim_a, claim_a.lease_token)
    assert first_report["attachments"]["failed"] == 1
    assert db.get(ImportJob, claim_a.id).status == "partial"

    monkeypatch.setattr(ArchiveService, "prepare", original_prepare)
    assert ImportWorker(db).retry(claim_a.id, "generic-tree-fixture", "0.1")
    claim_b = ImportWorker(db).claim_next(60)
    second_report = ImportProcessor(db, root).process(claim_b, claim_b.lease_token)

    assert second_report["attachments"] == {
        "available": 1,
        "missing": 0,
        "referenced_only": 0,
        "failed": 0,
    }
    history = db.query(AttachmentObservation).order_by(AttachmentObservation.attempt_number).all()
    assert [(item.attempt_number, item.availability) for item in history] == [(1, "failed"), (2, "available")]
    assert db.query(Attachment).count() == 1
    archived = db.get(ArchiveObject, history[-1].archive_object_id)
    manifest = json.loads((root / "manifests" / f"{archived.sha256}.json").read_text(encoding="utf-8"))
    assert manifest["sha256"] == archived.sha256 == hashlib.sha256(b"retry bytes").hexdigest()


def test_stale_recovery_uses_committed_retry_attempt_attachment_truth(clean_db, tmp_path, monkeypatch):
    db = clean_db
    root = tmp_path / "data"
    payload = _payload([{"id": "recover", "member_path": "files/recover.bin"}])
    claim_a, _account, _archive = _claim(db, root, payload, {"files/recover.bin": b"recover"})
    original_prepare = ArchiveService.prepare
    monkeypatch.setattr(
        ArchiveService,
        "prepare",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("attempt one failed")),
    )
    ImportProcessor(db, root).process(claim_a, claim_a.lease_token)
    job = db.get(ImportJob, claim_a.id)
    job.max_attempts = 2
    db.commit()
    monkeypatch.setattr(ArchiveService, "prepare", original_prepare)
    assert ImportWorker(db).retry(claim_a.id, "generic-tree-fixture", "0.1")
    claim_b = ImportWorker(db).claim_next(60)

    def expire_after_business_commit(_worker, job_id, token, *args, **kwargs):
        with Session(engine) as lifecycle:
            lifecycle.execute(
                update(ImportJob).where(ImportJob.id == job_id).values(
                    lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)
                )
            )
            lifecycle.commit()
        raise LeaseLost("crash_before_terminal_update")

    monkeypatch.setattr(ImportWorker, "mark_completed", expire_after_business_commit)
    with pytest.raises(LeaseLost, match="crash_before_terminal_update"):
        ImportProcessor(db, root).process(claim_b, claim_b.lease_token)

    assert ImportWorker(db).recover_stale() == 1
    recovered = db.get(ImportJob, claim_b.id)
    assert recovered.report_json["attachments"] == {
        "available": 1,
        "missing": 0,
        "referenced_only": 0,
        "failed": 0,
    }
    history = db.query(AttachmentObservation).order_by(AttachmentObservation.attempt_number).all()
    assert [(item.attempt_number, item.availability) for item in history] == [(1, "failed"), (2, "available")]


def test_attachment_reimport_same_attempt_is_idempotent(clean_db, tmp_path):
    db = clean_db
    root = tmp_path / "data"
    payload = _payload([{"id": "idempotent", "member_path": "files/item.bin"}])
    claim, _account, _archive = _claim(db, root, payload, {"files/item.bin": b"item"})
    processor = ImportProcessor(db, root)
    first = processor.process(claim, claim.lease_token)
    # Exercise service replay before terminal ownership is released by recreating
    # an equivalent owned state for the same attempt and token.
    db.execute(update(ImportJob).where(ImportJob.id == claim.id).values(
        status="running",
        lease_token=claim.lease_token,
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=60),
    ))
    db.commit()
    archived_id = db.query(AttachmentObservation).one().archive_object_id
    archived = db.get(ArchiveObject, archived_id)
    manifest_path = root / "manifests" / f"{archived.sha256}.json"
    manifest_path.unlink()
    second = processor.process(claim, claim.lease_token)

    assert first["attachments"] == second["attachments"]
    assert db.query(Attachment).count() == 1
    assert db.query(AttachmentObservation).count() == 1
    assert db.query(ArchiveObservation).filter_by(import_job_id=claim.id).count() == 1
    assert db.query(ImportBatchCommit).count() == 1
    assert manifest_path.exists()


def test_same_bytes_with_distinct_source_identities_deduplicate_object_not_provenance(clean_db, tmp_path):
    db = clean_db
    root = tmp_path / "data"
    payload = _payload([
        {"id": "first", "member_path": "files/first.bin"},
        {"id": "second", "member_path": "files/second.bin"},
    ])
    claim, _account, input_archive = _claim(
        db,
        root,
        payload,
        {"files/first.bin": b"shared bytes", "files/second.bin": b"shared bytes"},
    )

    report = ImportProcessor(db, root).process(claim, claim.lease_token)

    observations = db.query(AttachmentObservation).all()
    object_ids = {item.archive_object_id for item in observations}
    assert report["attachments"]["available"] == 2
    assert db.query(Attachment).count() == 2
    assert len(observations) == 2 and len(object_ids) == 1
    assert next(iter(object_ids)) != input_archive.id
    assert db.query(ArchiveObservation).filter_by(import_job_id=claim.id).count() == 2
    provenance = db.query(ArchiveObservation).filter_by(import_job_id=claim.id).all()
    assert {item.metadata_json["stable_attachment_key"] for item in provenance} == {
        "external:first",
        "external:second",
    }
    assert {item.metadata_json["attempt_number"] for item in provenance} == {1}


def test_stale_attachment_worker_leaves_only_orphan_bytes_then_new_worker_reuses_it(clean_db, tmp_path, monkeypatch):
    db = clean_db
    root = tmp_path / "data"
    payload = _payload([{"id": "orphan", "member_path": "files/orphan.bin"}])
    attachment_bytes = b"published before lease loss"
    claim_a, _account, _archive = _claim(db, root, payload, {"files/orphan.bin": attachment_bytes})
    original_prepare = ArchiveService.prepare
    published = {}

    def steal_after_prepare(service, *args, **kwargs):
        prepared = original_prepare(service, *args, **kwargs)
        published["prepared"] = prepared
        with Session(engine) as thief:
            thief.execute(update(ImportJob).where(ImportJob.id == claim_a.id).values(
                lease_token=uuid.uuid4(),
                lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
            ))
            thief.commit()
        return prepared

    monkeypatch.setattr(ArchiveService, "prepare", steal_after_prepare)
    with pytest.raises(LeaseLost):
        ImportProcessor(db, root).process(claim_a, claim_a.lease_token)

    prepared = published["prepared"]
    assert (root / prepared.relative_path).read_bytes() == attachment_bytes
    assert db.scalar(select(ArchiveObject).where(ArchiveObject.sha256 == prepared.sha256)) is None
    assert db.query(ArchiveObservation).count() == 0
    assert db.query(Attachment).count() == 0
    assert db.query(AttachmentObservation).count() == 0
    assert db.query(ImportBatchCommit).count() == 0

    monkeypatch.setattr(ArchiveService, "prepare", original_prepare)
    assert ImportWorker(db).recover_stale() == 1
    claim_b = ImportWorker(db).claim_next(60)
    report = ImportProcessor(db, root).process(claim_b, claim_b.lease_token)
    observation = db.query(AttachmentObservation).one()
    archived = db.get(ArchiveObject, observation.archive_object_id)
    assert report["attachments"]["available"] == 1
    assert archived.sha256 == prepared.sha256
    assert (root / archived.relative_path).read_bytes() == attachment_bytes
    manifest = json.loads((root / "manifests" / f"{archived.sha256}.json").read_text(encoding="utf-8"))
    assert manifest["sha256"] == archived.sha256
