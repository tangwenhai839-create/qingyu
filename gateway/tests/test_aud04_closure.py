import json
import time
import uuid
import zipfile
from datetime import UTC, datetime, timedelta

import app.importers.processor as processor_module
import pytest
from app.archive.service import ArchiveService
from app.db import engine
from app.importers.processor import ArchiveConsumptionIntegrityError, ImportProcessor
from app.importers.service import ImportService
from app.importers.worker import ImportWorker, LeaseLost, LeaseRequired
from app.models import (
    ArchiveObject,
    Attachment,
    Conversation,
    ImportJob,
    Message,
    MessageObservation,
    MessageRevision,
    SourceAccount,
)
from sqlalchemy import select, update
from sqlalchemy.orm import Session


def _job(db):
    account = SourceAccount(platform="aud04", account_label=str(uuid.uuid4()), metadata_json={})
    archive = ArchiveObject(sha256=uuid.uuid4().hex * 2, size_bytes=1, relative_path="unused", media_type="application/json", integrity_status="verified", metadata_json={})
    db.add_all([account, archive]); db.flush()
    job = ImportJob(source_account_id=account.id, archive_object_id=archive.id, parser_name="fixture", parser_version="0.1", status="pending", report_json={})
    db.add(job); db.commit()
    return job, archive


def _payload(conversation_id):
    return {"schema_marker": "qingyu-fixture-v1", "conversations": [{"id": conversation_id, "mapping": {}, "current_node": None}]}


def test_database_wall_clock_expires_inside_existing_transaction():
    with Session(engine) as db:
        job, _ = _job(db); worker = ImportWorker(db); job = worker.claim_next(1); token = job.lease_token
        worker.assert_ownership(job, token)
        time.sleep(1.2)
        with pytest.raises(LeaseLost): worker.assert_ownership(job, token)
        assert worker.heartbeat(job, token) is False


def test_missing_token_cannot_write_business_data():
    with Session(engine) as db:
        job, archive = _job(db); job = ImportWorker(db).claim_next(60)
        identity = f"blocked-{uuid.uuid4()}"
        with pytest.raises(LeaseRequired):
            ImportService(db).import_fixture(_payload(identity), archive.id, job=job, lease_token=None)
        assert db.query(Conversation).filter_by(external_conversation_id=identity).count() == 0


def test_stale_worker_cannot_overwrite_new_worker_final_state():
    with Session(engine) as db:
        job, _ = _job(db); worker = ImportWorker(db); job = worker.claim_next(60); token_a = job.lease_token
        token_b = uuid.uuid4()
        db.execute(update(ImportJob).where(ImportJob.id == job.id).values(lease_token=token_b, report_json={"owner": "B"}, items_imported=9, result_code="B")); db.commit()
        assert worker.mark_completed(job, token_a, {"owner": "A"}, "A", {"items_imported": 1}) is False
        assert worker.mark_failed(job, token_a, "A-error", {"owner": "A"}, "A") is False
        db.expire_all(); current = db.get(ImportJob, job.id)
        assert current.report_json == {"owner": "B"}
        assert current.items_imported == 9 and current.result_code == "B" and current.lease_token == token_b


def test_failed_fenced_helper_does_not_commit_callers_dirty_state():
    with Session(engine, autoflush=False) as caller:
        job, archive = _job(caller)
        job = ImportWorker(caller).claim_next(60)
        archive.integrity_error = "must-not-be-committed"
        assert archive in caller.dirty

        assert ImportWorker(caller).mark_failed(
            job.id,
            uuid.uuid4(),
            "stale-worker",
            {"owner": "stale"},
        ) is False

        with Session(engine) as observer:
            assert observer.get(ArchiveObject, archive.id).integrity_error is None
        assert archive in caller.dirty
        caller.rollback()


def test_expired_worker_has_zero_integrity_side_effects_then_new_owner_records_failure(tmp_path, monkeypatch):
    root = tmp_path / "data"
    source = tmp_path / "fixture.zip"
    payload = {
        "schema_marker": "qingyu-fixture-v1",
        "conversations": [{
            "id": f"integrity-{uuid.uuid4()}",
            "mapping": {"n": {"parent": None, "message": {"author": {"role": "user"}, "content": {"parts": ["abc"]}}}},
        }],
    }
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("fixture.json", json.dumps(payload))

    with Session(engine, expire_on_commit=False) as db:
        archived = ArchiveService(root, db).archive_file(source, media_type="application/zip")
        account = SourceAccount(platform="aud04-integrity", account_label=str(uuid.uuid4()), metadata_json={})
        db.add(account); db.flush()
        token_a = uuid.uuid4()
        job = ImportJob(
            source_account_id=account.id,
            archive_object_id=archived.object_id,
            parser_name="fixture",
            parser_version="0.1",
            status="running",
            lease_token=token_a,
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=1),
            report_json={"owner": "before"},
            items_discovered=7,
            items_imported=5,
            result_code="before-result",
            error_code="before-error",
        )
        db.add(job); db.commit()
        job_id, archive_id = job.id, archived.object_id

        replacement = tmp_path / "replacement.zip"
        payload["conversations"][0]["mapping"]["n"]["message"]["content"]["parts"] = ["xyz"]
        with zipfile.ZipFile(replacement, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("fixture.json", json.dumps(payload))
        assert replacement.stat().st_size == source.stat().st_size
        (root / archived.relative_path).write_bytes(replacement.read_bytes())

        original_copy = processor_module.copy_and_verify_archive
        def copy_then_expire(*args):
            result = original_copy(*args)
            time.sleep(1.2)
            return result
        monkeypatch.setattr(processor_module, "copy_and_verify_archive", copy_then_expire)

        with pytest.raises(LeaseLost):
            ImportProcessor(db, root).process(job, token_a)

    with Session(engine) as verify_a:
        current_job = verify_a.get(ImportJob, job_id)
        current_archive = verify_a.get(ArchiveObject, archive_id)
        assert current_archive.integrity_status == "verified"
        assert current_archive.integrity_error is None
        assert current_job.status == "running"
        assert current_job.report_json == {"owner": "before"}
        assert current_job.items_discovered == 7
        assert current_job.items_imported == 5
        assert current_job.result_code == "before-result"
        assert current_job.error_code == "before-error"
        assert json.loads(archived.manifest_path.read_text(encoding="utf-8"))["integrity_status"] == "verified"

        token_b = uuid.uuid4()
        verify_a.execute(
            update(ImportJob)
            .where(ImportJob.id == job_id)
            .values(lease_token=token_b, lease_expires_at=datetime.now(UTC) + timedelta(seconds=60))
        )
        verify_a.commit()

    monkeypatch.setattr(processor_module, "copy_and_verify_archive", original_copy)
    with Session(engine, expire_on_commit=False) as worker_b:
        job_b = worker_b.get(ImportJob, job_id)
        with pytest.raises(ArchiveConsumptionIntegrityError):
            ImportProcessor(worker_b, root).process(job_b, token_b)

    with Session(engine) as verify_b:
        current_job = verify_b.get(ImportJob, job_id)
        current_archive = verify_b.get(ArchiveObject, archive_id)
        assert current_archive.integrity_status == "integrity_failure"
        assert current_archive.integrity_error.startswith("archive_integrity_failure:")
        assert current_archive.integrity_checked_at is not None
        assert current_job.status == "failed"
        assert current_job.result_code == "archive_integrity_failure"
        assert current_job.error_code == "archive_integrity_failure"
        manifest = json.loads(archived.manifest_path.read_text(encoding="utf-8"))
        assert manifest["expected_sha256"] == current_archive.sha256
        assert manifest["integrity_status"] == "integrity_failure"
        assert manifest["integrity_checked_at"] is not None
        assert manifest["integrity_error"] == current_archive.integrity_error
        assert verify_b.scalars(select(Conversation).where(Conversation.source_account_id == current_job.source_account_id)).all() == []
        assert verify_b.query(Message).join(Conversation).filter(Conversation.source_account_id == current_job.source_account_id).count() == 0
        assert verify_b.query(MessageRevision).filter(MessageRevision.archive_object_id == archive_id).count() == 0
        assert verify_b.query(MessageObservation).filter(MessageObservation.import_job_id == job_id).count() == 0
        assert verify_b.query(Attachment).join(MessageRevision).filter(MessageRevision.archive_object_id == archive_id).count() == 0
