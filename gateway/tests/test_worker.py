from datetime import UTC


def test_worker_module_imports():
    from app.importers.worker import ImportWorker
    assert ImportWorker is not None

def test_stale_job_is_requeued_or_failed():
    from datetime import datetime, timedelta

    from app.db import engine
    from app.importers.worker import ImportWorker
    from app.models import ArchiveObject, ImportJob, SourceAccount
    from sqlalchemy.orm import Session
    with Session(engine) as db:
        account = SourceAccount(platform="test-worker", account_label="w", metadata_json={})
        archive = ArchiveObject(sha256=__import__('uuid').uuid4().hex * 2, size_bytes=0, relative_path="x", media_type="x", integrity_status="verified", metadata_json={})
        db.add_all([account, archive]); db.flush()
        job = ImportJob(source_account_id=account.id, archive_object_id=archive.id, parser_name="x", parser_version="x", status="running", attempt_count=1, max_attempts=3, lease_expires_at=datetime.now(UTC)-timedelta(seconds=1), report_json={})
        db.add(job); db.commit()
        assert ImportWorker(db).recover_stale() >= 1
        db.refresh(job)
        assert job.status == "pending"

def test_old_token_cannot_update_or_own_job():
    from datetime import datetime, timedelta

    from app.db import engine
    from app.importers.worker import ImportWorker, LeaseLost
    from app.models import ArchiveObject, ImportJob, SourceAccount
    from sqlalchemy.orm import Session
    with Session(engine) as db:
        account = SourceAccount(platform="fence", account_label="w", metadata_json={})
        archive = ArchiveObject(sha256=__import__('uuid').uuid4().hex * 2, size_bytes=0, relative_path="y", media_type="x", integrity_status="verified", metadata_json={})
        db.add_all([account, archive]); db.flush()
        job = ImportJob(source_account_id=account.id, archive_object_id=archive.id, parser_name="x", parser_version="x", status="pending", report_json={})
        db.add(job); db.commit()
        worker = ImportWorker(db); claimed = worker.claim_next(60); old = claimed.lease_token
        db.execute(__import__('sqlalchemy').update(ImportJob).where(ImportJob.id == claimed.id).values(lease_token=__import__('uuid').uuid4(), lease_expires_at=datetime.now(UTC)+timedelta(seconds=60))); db.commit()
        assert worker.heartbeat(claimed, old) is False
        try: worker.assert_ownership(claimed, old)
        except LeaseLost: pass
        else: assert False
