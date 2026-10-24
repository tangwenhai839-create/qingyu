import os
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db import engine
from app.importers.cleanup import cleanup_failed_staging
from app.importers.generic import GenericTreeParser
from app.importers.service import BatchReplayIdentityError, ImportService
from app.importers.status import CANCELLED, COMPLETED, FAILED, PARTIAL, PENDING, RUNNING
from app.importers.worker import ImportWorker, LeaseLost
from app.main import app
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
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, inspect, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

HEADERS = {"Authorization": "Bearer qingyu-test-token-only"}


def _clear(db):
    for model in (
        ImportBatchCommit, Attachment, MessageObservation, MessageRevision,
        Message, Conversation, ImportJob, SourceAccount, ArchiveObject,
    ):
        db.execute(delete(model))
    db.commit()


@pytest.fixture()
def clean_db():
    with Session(engine, expire_on_commit=False) as db:
        _clear(db)
        yield db
        _clear(db)


def _pending_job(db, status=PENDING, attempt_count=0):
    account = SourceAccount(platform="checkpoint", account_label=str(uuid.uuid4()), metadata_json={})
    archive = ArchiveObject(
        sha256=uuid.uuid4().hex * 2,
        size_bytes=1,
        relative_path="unused",
        media_type="application/zip",
        integrity_status="verified",
        metadata_json={},
    )
    db.add_all([account, archive]); db.flush()
    job = ImportJob(
        source_account_id=account.id,
        archive_object_id=archive.id,
        parser_name=GenericTreeParser.name,
        parser_version=GenericTreeParser.version,
        status=status,
        attempt_count=attempt_count,
        report_json={},
    )
    db.add(job); db.commit()
    return job


def _conversation(conversation_id):
    return {
        "id": conversation_id,
        "title": conversation_id,
        "mapping": {
            "node": {
                "parent": None,
                "message": {"author": {"role": "user"}, "content": {"parts": [conversation_id]}},
            }
        },
    }


def _payload(*conversation_ids):
    return {"schema_marker": "qingyu-fixture-v1", "conversations": [_conversation(value) for value in conversation_ids]}


def test_batch_ledger_rolls_back_with_business_transaction(clean_db):
    db = clean_db; job = _pending_job(db); claim = ImportWorker(db).claim_next(60)

    def reject_ledger(session, _context, _instances):
        if any(isinstance(item, ImportBatchCommit) for item in session.new):
            raise RuntimeError("ledger_insert_failed")

    event.listen(db, "before_flush", reject_ledger)
    try:
        with pytest.raises(RuntimeError, match="ledger_insert_failed"):
            ImportService(db).import_fixture(_payload("atomic"), job.archive_object_id, job=claim, lease_token=claim.lease_token)
    finally:
        event.remove(db, "before_flush", reject_ledger)
    assert db.query(Conversation).count() == 0
    assert db.query(Message).count() == 0
    assert db.query(ImportBatchCommit).count() == 0
    assert db.get(ImportJob, job.id).checkpoint_sequence == 0


def test_checkpoint_lags_safely_then_retry_replays_without_duplicates(clean_db, monkeypatch):
    db = clean_db; job = _pending_job(db); claim_a = ImportWorker(db).claim_next(60)
    original = ImportWorker.advance_checkpoint
    monkeypatch.setattr(ImportWorker, "advance_checkpoint", lambda *args, **kwargs: False)
    with pytest.raises(LeaseLost):
        ImportService(db).import_fixture(_payload("replay"), job.archive_object_id, job=claim_a, lease_token=claim_a.lease_token)
    db.expire_all()
    assert db.query(ImportBatchCommit).count() == 1
    assert db.query(Conversation).count() == db.query(Message).count() == 1
    assert db.get(ImportJob, job.id).checkpoint_sequence == 0

    monkeypatch.setattr(ImportWorker, "advance_checkpoint", original)
    current = db.get(ImportJob, job.id)
    current.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    assert ImportWorker(db).recover_stale() == 1
    claim_b = ImportWorker(db).claim_next(60)
    ImportService(db).import_fixture(_payload("replay"), job.archive_object_id, job=claim_b, lease_token=claim_b.lease_token)
    db.expire_all()
    assert db.query(ImportBatchCommit).count() == 1
    assert db.query(Conversation).count() == db.query(Message).count() == 1
    assert db.get(ImportJob, job.id).checkpoint_sequence == 1


def test_checkpoint_is_monotonic_fenced_and_requires_durable_batch(clean_db):
    db = clean_db; job = _pending_job(db); claim = ImportWorker(db).claim_next(60)
    assert not ImportWorker(db).advance_checkpoint(job.id, claim.lease_token, 1, "missing")
    ImportService(db).import_fixture(_payload("one"), job.archive_object_id, job=claim, lease_token=claim.lease_token)
    db.expire_all()
    assert db.get(ImportJob, job.id).checkpoint_sequence == 1
    assert ImportWorker(db).advance_checkpoint(job.id, claim.lease_token, 1, "fixture.json:conversation:0")
    assert not ImportWorker(db).advance_checkpoint(job.id, uuid.uuid4(), 2, "fixture.json:conversation:1")
    db.expire_all()
    assert db.get(ImportJob, job.id).checkpoint_sequence == 1


class _FailAfterFirst(GenericTreeParser):
    def parse(self, payload):
        iterator = super().parse(payload)
        yield next(iterator)
        raise RuntimeError("second_batch_failed")


def test_partial_status_depends_on_durable_ledger_not_checkpoint(clean_db, monkeypatch):
    db = clean_db; job = _pending_job(db); claim = ImportWorker(db).claim_next(60)
    monkeypatch.setattr(ImportWorker, "advance_checkpoint", lambda *args, **kwargs: False)
    with pytest.raises(LeaseLost):
        ImportService(db).import_fixture(_payload("first", "second"), job.archive_object_id, job=claim, lease_token=claim.lease_token, parser=_FailAfterFirst())
    status = ImportWorker(db).mark_failed_or_partial(job.id, claim.lease_token, "processor_error", {"safe": True})
    db.expire_all(); current = db.get(ImportJob, job.id)
    assert status == PARTIAL == current.status
    assert current.checkpoint_sequence == 0
    assert current.imported_count == 1
    assert current.report_json["durable_batches"] == 1
    assert db.query(ImportBatchCommit).count() == 1


def test_failure_without_durable_ledger_is_failed(clean_db):
    db = clean_db; job = _pending_job(db); claim = ImportWorker(db).claim_next(60)
    status = ImportWorker(db).mark_failed_or_partial(job.id, claim.lease_token, "parser_error", {"safe": True})
    assert status == FAILED
    db.expire_all()
    assert db.get(ImportJob, job.id).status == FAILED


@pytest.mark.parametrize("change", ["conversation", "parser", "contract"])
def test_batch_replay_validates_identity_parser_and_contract(clean_db, change):
    db = clean_db; job = _pending_job(db); claim = ImportWorker(db).claim_next(60)
    ImportService(db).import_fixture(_payload("identity"), job.archive_object_id, job=claim, lease_token=claim.lease_token)

    class ChangedParser(GenericTreeParser):
        name = "changed-parser" if change == "parser" else GenericTreeParser.name
        version = "changed-contract" if change == "contract" else GenericTreeParser.version

    payload = _payload("changed" if change == "conversation" else "identity")
    with pytest.raises(BatchReplayIdentityError):
        ImportService(db).import_fixture(payload, job.archive_object_id, job=claim, lease_token=claim.lease_token, parser=ChangedParser())
    assert db.query(ImportBatchCommit).count() == 1
    assert db.query(Conversation).count() == 1


@pytest.mark.parametrize("initial_status", [FAILED, PARTIAL])
def test_retry_api_is_atomic_and_attempt_increments_only_on_claim(clean_db, initial_status):
    db = clean_db; job = _pending_job(db, initial_status, attempt_count=2); job_id = job.id
    barrier = threading.Barrier(2)

    def retry_once():
        barrier.wait()
        with TestClient(app) as client:
            return client.post(f"/api/v1/import-jobs/{job_id}/retry", headers=HEADERS).status_code

    threads = []
    results = []
    for _ in range(2):
        thread = threading.Thread(target=lambda: results.append(retry_once()))
        threads.append(thread); thread.start()
    for thread in threads: thread.join(timeout=5)
    assert sorted(results) == [202, 409]
    db.expire_all(); current = db.get(ImportJob, job_id)
    assert current.status == PENDING and current.attempt_count == 2
    claim = ImportWorker(db).claim_next(60)
    assert claim.job_id == job_id and claim.attempt_count == 3


def test_explicit_retry_completes_remaining_batches_by_replaying_ledger(clean_db):
    db = clean_db; job = _pending_job(db); claim_a = ImportWorker(db).claim_next(60)
    with pytest.raises(RuntimeError, match="second_batch_failed"):
        ImportService(db).import_fixture(
            _payload("first", "second"), job.archive_object_id,
            job=claim_a, lease_token=claim_a.lease_token, parser=_FailAfterFirst(),
        )
    assert ImportWorker(db).mark_failed_or_partial(job.id, claim_a.lease_token, "processor_error") == PARTIAL
    response = TestClient(app).post(f"/api/v1/import-jobs/{job.id}/retry", headers=HEADERS)
    assert response.status_code == 202
    db.expire_all()
    claim_b = ImportWorker(db).claim_next(60)
    outcome = ImportService(db).import_fixture(
        _payload("first", "second"), job.archive_object_id,
        job=claim_b, lease_token=claim_b.lease_token,
    )
    assert ImportWorker(db).mark_completed(job.id, claim_b.lease_token, outcome.report, counts=outcome.counts)
    db.expire_all(); current = db.get(ImportJob, job.id)
    assert current.status == COMPLETED and current.checkpoint_sequence == 2
    assert current.imported_count == 2
    assert db.query(ImportBatchCommit).count() == 2
    assert db.query(Conversation).count() == db.query(Message).count() == 2


@pytest.mark.parametrize("job_status", [PENDING, RUNNING, COMPLETED, CANCELLED])
def test_retry_api_rejects_non_retryable_statuses(clean_db, job_status):
    db = clean_db; job = _pending_job(db, job_status)
    response = TestClient(app).post(f"/api/v1/import-jobs/{job.id}/retry", headers=HEADERS)
    assert response.status_code == 409


@pytest.mark.parametrize("initial_status", [FAILED, PARTIAL])
def test_retry_api_rejects_exhausted_jobs(clean_db, initial_status):
    db = clean_db
    job = _pending_job(db, initial_status, attempt_count=3)
    job.max_attempts = 3
    db.commit()

    response = TestClient(app).post(f"/api/v1/import-jobs/{job.id}/retry", headers=HEADERS)

    assert response.status_code == 409
    db.expire_all()
    current = db.get(ImportJob, job.id)
    assert current.status == initial_status
    assert current.attempt_count == current.max_attempts == 3


def test_claim_skips_pending_job_with_exhausted_attempts(clean_db):
    db = clean_db
    exhausted = _pending_job(db, attempt_count=3)
    exhausted.max_attempts = 3
    db.commit()

    assert ImportWorker(db).claim_next(60) is None
    db.expire_all()
    current = db.get(ImportJob, exhausted.id)
    assert current.status == PENDING
    assert current.attempt_count == current.max_attempts == 3


@pytest.mark.parametrize("failure_field", ["error_code", "result_code"])
def test_retry_rejects_archive_integrity_failure_atomically(clean_db, failure_field):
    db = clean_db
    job = _pending_job(db, FAILED, attempt_count=1)
    setattr(job, failure_field, "archive_integrity_failure")
    db.commit()

    assert not ImportWorker(db).retry(job.id)
    db.expire_all()
    assert db.get(ImportJob, job.id).status == FAILED


def test_retry_rejects_job_whose_archive_is_no_longer_verified(clean_db):
    db = clean_db
    job = _pending_job(db, FAILED, attempt_count=1)
    archive = db.get(ArchiveObject, job.archive_object_id)
    archive.integrity_status = "integrity_failure"
    db.commit()

    assert not ImportWorker(db).retry(job.id)
    db.expire_all()
    assert db.get(ImportJob, job.id).status == FAILED


@pytest.mark.parametrize("mismatch", ["name", "version"])
def test_retry_rejects_parser_identity_mismatch_atomically(clean_db, mismatch):
    db = clean_db
    job = _pending_job(db, PARTIAL, attempt_count=1)

    expected_name = "other-parser" if mismatch == "name" else job.parser_name
    expected_version = "other-version" if mismatch == "version" else job.parser_version

    assert not ImportWorker(db).retry(job.id, expected_name, expected_version)
    db.expire_all()
    assert db.get(ImportJob, job.id).status == PARTIAL


def test_failed_job_without_durable_batches_can_retry_with_current_parser(clean_db):
    db = clean_db
    job = _pending_job(db, FAILED, attempt_count=1)
    job.parser_name = "retired-parser"
    job.parser_version = "retired-version"
    db.commit()

    assert ImportWorker(db).retry(
        job.id,
        GenericTreeParser.name,
        GenericTreeParser.version,
    )
    db.expire_all()
    assert db.get(ImportJob, job.id).status == PENDING


def test_exhausted_stale_job_uses_ledger_for_partial(clean_db):
    db = clean_db; job = _pending_job(db); claim = ImportWorker(db).claim_next(60)
    ImportService(db).import_fixture(_payload("durable"), job.archive_object_id, job=claim, lease_token=claim.lease_token)
    current = db.get(ImportJob, job.id); current.max_attempts = current.attempt_count; current.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1); db.commit()
    assert ImportWorker(db).recover_stale() == 1
    db.expire_all(); current = db.get(ImportJob, job.id)
    assert current.status == PARTIAL and current.result_code == PARTIAL
    assert current.discovered_count == current.items_discovered == 1
    assert current.imported_count == current.items_imported == 1
    assert current.skipped_count == current.items_skipped == 0
    assert current.failed_count == current.items_failed == 0
    assert current.report_json["durable_batches"] == 1
    assert current.report_json["durable_counts"] == {
        "discovered_count": 1,
        "imported_count": 1,
        "skipped_count": 0,
        "failed_count": 0,
    }


def test_stale_worker_cannot_checkpoint_or_finalize_partial(clean_db):
    db = clean_db; job = _pending_job(db); claim_a = ImportWorker(db).claim_next(60)
    ImportService(db).import_fixture(_payload("owned"), job.archive_object_id, job=claim_a, lease_token=claim_a.lease_token)
    current = db.get(ImportJob, job.id); current.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1); db.commit()
    assert ImportWorker(db).recover_stale() == 1
    claim_b = ImportWorker(db).claim_next(60)
    assert not ImportWorker(db).advance_checkpoint(job.id, claim_a.lease_token, 1, "fixture.json:conversation:0")
    with pytest.raises(LeaseLost):
        ImportWorker(db).mark_failed_or_partial(job.id, claim_a.lease_token, "old_worker", {"owner": "A"})
    assert not ImportWorker(db).mark_completed(job.id, claim_a.lease_token, {"owner": "A"})
    db.expire_all(); current = db.get(ImportJob, job.id)
    assert current.status == RUNNING and current.lease_token == claim_b.lease_token
    assert current.report_json != {"owner": "A"}


def test_partial_and_cancelled_are_terminal_for_cleanup(clean_db, tmp_path):
    db = clean_db; paths = []
    for status in (PARTIAL, CANCELLED):
        job = _pending_job(db, status); token = uuid.uuid4()
        path = tmp_path / "staging" / str(job.id) / "1" / str(token)
        path.mkdir(parents=True); (path / "diagnostic").write_text("x")
        old = time.time() - 10 * 86400; os.utime(path, (old, old)); paths.append(path.resolve())
    result = cleanup_failed_staging(tmp_path, db, 7)
    assert set(result.deleted) == set(paths)


def test_commit2_schema_matches_model_and_rejects_unknown_job_status(clean_db):
    db = clean_db; inspector = inspect(engine)
    columns = {column["name"]: column for column in inspector.get_columns("import_jobs")}
    assert columns["checkpoint_sequence"]["nullable"] is False
    assert "0" in str(columns["checkpoint_sequence"]["default"])
    assert "import_batch_commits" in inspector.get_table_names()
    uniques = {item["name"] for item in inspector.get_unique_constraints("import_batch_commits")}
    assert {
        "uq_import_batch_commits_job_key",
        "uq_import_batch_commits_job_sequence",
        "uq_import_batch_commits_id_conversation",
    }.issubset(uniques)
    checks = {item["name"] for item in inspector.get_check_constraints("import_jobs")}
    assert "ck_import_jobs_status" in checks
    job = _pending_job(db)
    with pytest.raises(IntegrityError):
        db.execute(update(ImportJob).where(ImportJob.id == job.id).values(status="unknown"))
        db.commit()
    db.rollback()
