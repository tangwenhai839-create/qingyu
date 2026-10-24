import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db import engine
from app.importers.service import ImportService
from app.importers.worker import ClaimedJob, ImportWorker, LeaseLost
from app.models import (
    ArchiveObject,
    Attachment,
    Conversation,
    ImportBatchCommit,
    ImportJob,
    Message,
    MessageObservation,
    MessageRevision,
    SourceAccount,
)
from sqlalchemy import delete, update
from sqlalchemy.orm import Session


def fixture_payload(cid="c2"):
    return {"schema_marker":"qingyu-fixture-v1", "conversations": [{"id": cid, "title": "retry", "current_node": "n1", "mapping": {"n1": {"parent": None, "message": {"author": {"role": "user"}, "content": {"parts": ["hello"]}}}}}]}

@pytest.fixture()
def clean_db():
    with Session(engine) as db:
        for model in (Attachment, MessageObservation, MessageRevision, Message, Conversation, ImportJob, SourceAccount, ArchiveObject): db.execute(delete(model))
        archive = ArchiveObject(sha256=uuid.uuid4().hex * 2, size_bytes=1, relative_path="x", media_type="application/json", integrity_status="verified", metadata_json={})
        account = SourceAccount(platform="fenced-test", account_label=str(uuid.uuid4()), metadata_json={})
        db.add_all([archive, account]); db.flush()
        job = ImportJob(source_account_id=account.id, archive_object_id=archive.id, parser_name="fixture", parser_version="0.1", status="pending", report_json={})
        db.add(job); db.commit()
        yield db, job.id, archive.id

def test_old_worker_cannot_commit_business_side_effects(clean_db):
    db, job_id, archive_id = clean_db
    worker = ImportWorker(db); job = worker.claim_next(60); token_a = job.lease_token
    db.execute(update(ImportJob).where(ImportJob.id == job_id).values(lease_token=uuid.uuid4(), lease_expires_at=datetime.now(UTC) + timedelta(seconds=60))); db.commit()
    with pytest.raises(LeaseLost): ImportService(db).import_fixture(fixture_payload(), archive_id, job=job, lease_token=token_a)
    assert db.query(Conversation).filter_by(external_conversation_id="c2").count() == 0
    assert db.query(Message).count() == 0
    assert db.query(MessageRevision).count() == 0
    assert db.query(MessageObservation).count() == 0
    assert db.query(Attachment).count() == 0

def test_retry_reuses_stable_entities(clean_db):
    db, _job_id, archive_id = clean_db
    worker = ImportWorker(db); job = worker.claim_next(); token = job.lease_token
    service = ImportService(db); first = service.import_fixture(fixture_payload("retry-c"), archive_id, job=job, lease_token=token)
    second = service.import_fixture(fixture_payload("retry-c"), archive_id, job=job, lease_token=token)
    assert first.report["messages"]["new"] == 1 and second.report["messages"]["new"] == 1
    assert db.query(Conversation).filter_by(external_conversation_id="retry-c").count() == 1
    assert db.query(Message).count() == 1
    assert db.query(MessageRevision).count() == 1
    assert db.query(MessageObservation).count() == 1
    assert db.query(ImportBatchCommit).count() == 1

def test_checkpoint_only_after_business_commit(clean_db):
    db, _job_id, archive_id = clean_db
    worker = ImportWorker(db); job = worker.claim_next(); token = job.lease_token
    assert worker.advance_checkpoint(job, token, 1, "fixture.json:conversation:0") is False
    ImportService(db).import_fixture(fixture_payload("checkpoint"), archive_id, job=job, lease_token=token)
    db.expire_all(); assert db.get(ImportJob,job.id).checkpoint_sequence == 1
    assert worker.advance_checkpoint(job, uuid.uuid4(), 1, "fixture.json:conversation:0") is False
    db.expire_all(); assert db.get(ImportJob,job.id).checkpoint_sequence == 1

def test_attempt_staging_isolated(clean_db, tmp_path):
    db, _, _ = clean_db
    worker = ImportWorker(db); job = worker.claim_next(); first = worker.attempt_staging_path(tmp_path, job)
    token_b=uuid.uuid4()
    db.execute(update(ImportJob).where(ImportJob.id==job.id).values(lease_token=token_b,attempt_count=job.attempt_count+1,lease_expires_at=datetime.now(UTC)+timedelta(seconds=60))); db.commit()
    next_attempt=ClaimedJob(job_id=job.id,lease_token=token_b,attempt_count=job.attempt_count+1,source_account_id=job.source_account_id,archive_object_id=job.archive_object_id)
    second = worker.attempt_staging_path(tmp_path, next_attempt)
    assert first != second
    assert str(first).startswith(str(tmp_path)) and str(second).startswith(str(tmp_path))
