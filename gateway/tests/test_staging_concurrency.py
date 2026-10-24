"""Real-thread concurrency gates for staging cleanup ownership fences.

These tests intentionally use independent PostgreSQL sessions in each thread.
They must run serially with the rest of the database suite.
"""

import os
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import app.importers.cleanup as cleanup_module
import pytest
from app.db import engine
from app.importers.cleanup import cleanup_staging
from app.importers.staging import ensure_staging_layout, job_attempt_path
from app.importers.worker import ImportWorker
from app.models import ArchiveObject, ImportJob, SourceAccount
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session


def _age(path: Path, days: int = 10) -> None:
    timestamp = time.time() - days * 86400
    os.utime(path, (timestamp, timestamp))


@pytest.fixture
def concurrency_job():
    """Create one isolated job after an explicit destructive-test DB guard."""
    with Session(engine, expire_on_commit=False) as db:
        assert db.scalar(select(func.current_database())) == "qingyu_test"
        account = SourceAccount(
            platform="staging-concurrency",
            account_label=str(uuid.uuid4()),
            metadata_json={},
        )
        archive = ArchiveObject(
            sha256=uuid.uuid4().hex * 2,
            size_bytes=1,
            relative_path="unused",
            media_type="application/zip",
            integrity_status="verified",
            metadata_json={},
        )
        db.add_all([account, archive])
        db.flush()
        job = ImportJob(
            source_account_id=account.id,
            archive_object_id=archive.id,
            parser_name="generic-tree-fixture",
            parser_version="0.1",
            status="failed",
            attempt_count=1,
            created_at=datetime(2000, 1, 1, tzinfo=UTC),
            report_json={},
        )
        db.add(job)
        db.commit()
        ids = job.id, account.id, archive.id
    try:
        yield ids
    finally:
        with Session(engine) as db:
            assert db.scalar(select(func.current_database())) == "qingyu_test"
            db.execute(delete(ImportJob).where(ImportJob.id == ids[0]))
            db.execute(delete(SourceAccount).where(SourceAccount.id == ids[1]))
            db.execute(delete(ArchiveObject).where(ArchiveObject.id == ids[2]))
            db.commit()


def _old_attempt(root: Path, job_id: uuid.UUID, token: uuid.UUID) -> Path:
    path = job_attempt_path(root, job_id, 1, token, create=True)
    (path / "payload").write_bytes(b"old-attempt")
    _age(path)
    return path.resolve()


def _join(threads: list[threading.Thread], errors: list[BaseException]) -> None:
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads), "concurrency gate deadlocked"
    assert errors == []


def test_cleanup_vs_retry_cannot_delete_new_active_attempt(
    tmp_path: Path,
    concurrency_job,
    monkeypatch,
):
    job_id, _, _ = concurrency_job
    old_path = _old_attempt(tmp_path, job_id, uuid.uuid4())
    row_lock_reached = threading.Barrier(2)
    original_move = cleanup_module._move_to_quarantine
    results = {}
    errors = []

    def synchronize_after_cleanup_row_lock(path, base):
        if path == old_path:
            row_lock_reached.wait(timeout=5)
        return original_move(path, base)

    monkeypatch.setattr(cleanup_module, "_move_to_quarantine", synchronize_after_cleanup_row_lock)

    def cleanup_once():
        try:
            with Session(engine) as db:
                results["cleanup"] = cleanup_staging(tmp_path, db, retention_days=7)
        except BaseException as exc:  # noqa: BLE001  # surfaced in the parent test thread
            errors.append(exc)

    def retry_and_claim():
        try:
            row_lock_reached.wait(timeout=5)
            with Session(engine) as db:
                worker = ImportWorker(db)
                assert worker.retry(job_id)
                claim = worker.claim_next(lease_duration=60)
                assert claim is not None and claim.job_id == job_id
                results["claim"] = claim
                active = job_attempt_path(
                    tmp_path,
                    claim.job_id,
                    claim.attempt_count,
                    claim.lease_token,
                    create=True,
                )
                (active / "payload").write_bytes(b"new-active-attempt")
                results["active"] = active.resolve()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=cleanup_once), threading.Thread(target=retry_and_claim)]
    _join(threads, errors)

    claim = results["claim"]
    active_path = results["active"]
    assert not old_path.exists()
    assert active_path.exists()
    assert len(results["cleanup"].deleted) == 1
    assert results["cleanup"].failed == []
    with Session(engine) as db:
        current = db.get(ImportJob, job_id)
        assert current.status == "running"
        assert current.attempt_count == 2
        assert current.lease_token == claim.lease_token


def test_cleanup_vs_stale_recovery_and_reclaim_preserves_new_owner(
    tmp_path: Path,
    concurrency_job,
    monkeypatch,
):
    job_id, _, _ = concurrency_job
    stale_token = uuid.uuid4()
    stale_expiry = datetime.now(UTC) - timedelta(minutes=1)
    with Session(engine) as db:
        job = db.get(ImportJob, job_id)
        job.status = "running"
        job.lease_token = stale_token
        job.lease_expires_at = stale_expiry
        job.max_attempts = 3
        db.commit()
    stale_path = _old_attempt(tmp_path, job_id, stale_token)

    preflight_reached = threading.Barrier(2)
    original_classify = cleanup_module._classify_attempt
    classified = False
    results = {}
    errors = []

    def synchronize_cleanup_preflight(db, selected_job, attempt, token):
        nonlocal classified
        outcome = original_classify(db, selected_job, attempt, token)
        if selected_job == job_id and not classified:
            classified = True
            preflight_reached.wait(timeout=5)
        return outcome

    monkeypatch.setattr(cleanup_module, "_classify_attempt", synchronize_cleanup_preflight)

    def cleanup_once():
        try:
            with Session(engine) as db:
                results["cleanup"] = cleanup_staging(tmp_path, db, retention_days=7)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def recover_and_reclaim():
        try:
            preflight_reached.wait(timeout=5)
            with Session(engine) as db:
                worker = ImportWorker(db)
                assert worker.recover_observed(job_id, stale_token, stale_expiry, 1, 3)
                claim = worker.claim_next(lease_duration=60)
                assert claim is not None and claim.job_id == job_id
                results["claim"] = claim
                active = job_attempt_path(
                    tmp_path,
                    claim.job_id,
                    claim.attempt_count,
                    claim.lease_token,
                    create=True,
                )
                (active / "payload").write_bytes(b"reclaimed-attempt")
                results["active"] = active.resolve()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=cleanup_once), threading.Thread(target=recover_and_reclaim)]
    _join(threads, errors)

    claim = results["claim"]
    assert stale_path.exists()
    assert results["active"].exists()
    assert results["cleanup"].skipped_active == [stale_path]
    assert results["cleanup"].failed == []
    with Session(engine) as db:
        current = db.get(ImportJob, job_id)
        assert current.status == "running"
        assert current.attempt_count == 2
        assert current.lease_token == claim.lease_token
        assert current.lease_token != stale_token


def test_two_scanners_racing_quarantine_recovery_have_one_logical_owner(
    tmp_path: Path,
    concurrency_job,
    monkeypatch,
):
    layout = ensure_staging_layout(tmp_path)
    abandoned = (layout.quarantine / str(uuid.uuid4())).resolve()
    abandoned.mkdir()
    (abandoned / "payload").write_bytes(b"quarantined")
    rename_reached = threading.Barrier(2)
    original_replace = cleanup_module.os.replace
    results = []
    errors = []

    def synchronize_claim(source, destination):
        if Path(source).resolve() == abandoned:
            rename_reached.wait(timeout=5)
        return original_replace(source, destination)

    monkeypatch.setattr(cleanup_module.os, "replace", synchronize_claim)

    def recover_once():
        try:
            with Session(engine) as db:
                assert db.scalar(select(func.current_database())) == "qingyu_test"
                results.append(cleanup_staging(tmp_path, db, retention_days=7))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=recover_once), threading.Thread(target=recover_once)]
    _join(threads, errors)

    assert not abandoned.exists()
    assert sum(len(result.quarantine_recovered) for result in results) == 1
    assert all(result.failed == [] for result in results)
    assert list(layout.quarantine.iterdir()) == []
