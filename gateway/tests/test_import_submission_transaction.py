"""Crash-consistency tests for HTTP import submission."""

import hashlib
import uuid
from pathlib import Path

import app.importers.staging as staging_module
from app.config import get_settings
from app.db import engine, get_db
from app.main import app
from app.models import ArchiveObject, ArchiveObservation, ImportJob, SourceAccount
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

HEADERS = {"Authorization": "Bearer qingyu-test-token-only"}


def test_post_commit_cleanup_failure_does_not_mask_accepted_submission(tmp_path, monkeypatch):
    root = tmp_path / "archive-root"
    settings = get_settings()
    monkeypatch.setattr(settings, "archive_root", str(root))
    platform = f"cleanup-after-commit-{uuid.uuid4()}"
    monkeypatch.setattr(
        staging_module,
        "safe_remove_staging",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("private path")),
    )
    with Session(engine) as db:
        app.dependency_overrides[get_db] = lambda: db
        try:
            response = TestClient(app).post(
                "/api/v1/import/chatgpt",
                headers=HEADERS,
                data={"source_platform": platform},
                files={"file": ("export.zip", b"durable", "application/zip")},
            )
            assert response.status_code == 202
            jobs = list(db.scalars(select(ImportJob).join(SourceAccount).where(SourceAccount.platform == platform)))
            assert len(jobs) == 1
        finally:
            app.dependency_overrides.pop(get_db, None)
            db.rollback()
            account_ids = list(db.scalars(select(SourceAccount.id).where(SourceAccount.platform == platform)))
            object_ids = list(db.scalars(select(ImportJob.archive_object_id).where(ImportJob.source_account_id.in_(account_ids))))
            db.execute(delete(ArchiveObservation).where(ArchiveObservation.source_platform == platform))
            db.execute(delete(ImportJob).where(ImportJob.source_account_id.in_(account_ids)))
            db.execute(delete(SourceAccount).where(SourceAccount.id.in_(account_ids)))
            db.execute(delete(ArchiveObject).where(ArchiveObject.id.in_(object_ids)))
            db.commit()


def test_publish_then_submission_failure_rolls_back_all_business_rows_and_retry_reuses_bytes(
    tmp_path: Path,
    monkeypatch,
):
    root = tmp_path / "archive-root"
    settings = get_settings()
    monkeypatch.setattr(settings, "archive_root", str(root))
    source_platform = f"submission-crash-{uuid.uuid4()}"
    payload = b"published-before-transaction-failure"
    digest = hashlib.sha256(payload).hexdigest()
    published_path = root / "archive" / "objects" / digest[:2] / digest

    with Session(engine) as db:
        app.dependency_overrides[get_db] = lambda: db
        original_commit = db.commit

        def fail_before_commit():
            raise RuntimeError("simulated crash before durable submission")

        monkeypatch.setattr(db, "commit", fail_before_commit)
        try:
            failed = TestClient(app, raise_server_exceptions=False).post(
                "/api/v1/import/chatgpt",
                headers=HEADERS,
                data={"source_platform": source_platform},
                files={"file": ("export.zip", payload, "application/zip")},
            )
            assert failed.status_code == 500
            assert published_path.read_bytes() == payload
            assert db.scalar(select(ArchiveObject).where(ArchiveObject.sha256 == digest)) is None
            assert db.scalar(
                select(ArchiveObservation).where(
                    ArchiveObservation.source_platform == source_platform
                )
            ) is None
            assert db.scalar(
                select(ImportJob).join(SourceAccount).where(
                    SourceAccount.platform == source_platform
                )
            ) is None
            assert db.scalar(select(SourceAccount).where(SourceAccount.platform == source_platform)) is None

            monkeypatch.setattr(db, "commit", original_commit)
            accepted = TestClient(app).post(
                "/api/v1/import/chatgpt",
                headers=HEADERS,
                data={"source_platform": source_platform},
                files={"file": ("export.zip", payload, "application/zip")},
            )
            assert accepted.status_code == 202

            job = db.get(ImportJob, accepted.json()["import_job_id"])
            observation = db.scalar(
                select(ArchiveObservation).where(ArchiveObservation.import_job_id == job.id)
            )
            archive = db.get(ArchiveObject, job.archive_object_id)
            assert observation.archive_object_id == archive.id
            assert observation.source_platform == source_platform
            assert archive.sha256 == digest
            assert (root / archive.relative_path).read_bytes() == payload
        finally:
            app.dependency_overrides.pop(get_db, None)
            monkeypatch.setattr(db, "commit", original_commit)
            db.rollback()
            account_ids = list(
                db.scalars(select(SourceAccount.id).where(SourceAccount.platform == source_platform))
            )
            object_ids = list(
                db.scalars(
                    select(ArchiveObservation.archive_object_id).where(
                        ArchiveObservation.source_platform == source_platform
                    )
                )
            )
            db.execute(
                delete(ArchiveObservation).where(
                    ArchiveObservation.source_platform == source_platform
                )
            )
            db.execute(delete(ImportJob).where(ImportJob.source_account_id.in_(account_ids)))
            db.execute(delete(SourceAccount).where(SourceAccount.id.in_(account_ids)))
            db.execute(delete(ArchiveObject).where(ArchiveObject.id.in_(object_ids)))
            db.commit()
