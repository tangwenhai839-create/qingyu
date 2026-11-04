import hashlib
import io
import json
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.archive.manifest import sync_manifest_from_archive_object
from app.db import engine
from app.importers.worker import ImportWorker
from app.importers.worker_runtime import WorkerRuntime
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
from sqlalchemy import delete, select
from sqlalchemy.orm import Session


def _clear(db):
    for model in (Attachment, MessageObservation, MessageRevision, Message, Conversation, ImportJob, SourceAccount, ArchiveObject): db.execute(delete(model))
    db.commit()


def test_stale_recovery_cannot_overwrite_new_claim():
    with Session(engine) as db:
        _clear(db); account=SourceAccount(platform='race',account_label='a',metadata_json={}); archive=ArchiveObject(sha256='1'*64,size_bytes=1,relative_path='x',media_type='x',integrity_status='verified',metadata_json={}); db.add_all([account,archive]); db.flush()
        token_a=uuid.uuid4(); expiry=datetime.now(UTC)-timedelta(seconds=1); job=ImportJob(source_account_id=account.id,archive_object_id=archive.id,parser_name='x',parser_version='1',status='running',lease_token=token_a,lease_expires_at=expiry,attempt_count=1,max_attempts=3,report_json={}); db.add(job); db.commit(); observed=(job.id,token_a,expiry,1,3)
        assert ImportWorker(db).recover_stale()==1; claimed=ImportWorker(db).claim_next(60); token_b=claimed.lease_token
        assert ImportWorker(db).recover_observed(*observed) is False
        current=db.get(ImportJob,claimed.id); assert current.status=='running' and current.lease_token==token_b


def test_worker_runtime_processes_pending_zip(tmp_path: Path):
    root=tmp_path/'data'; payload={'schema_marker':'qingyu-fixture-v1','conversations':[{'id':'runtime-c','mapping':{}}]}; source=tmp_path/'source.zip'
    with zipfile.ZipFile(source,'w') as z: z.writestr('fixture.json',json.dumps(payload))
    content=source.read_bytes(); sha=hashlib.sha256(content).hexdigest(); target=root/'archive'/'objects'/sha[:2]/sha; target.parent.mkdir(parents=True); target.write_bytes(content)
    with Session(engine) as db:
        _clear(db); account=SourceAccount(platform='runtime',account_label='a',metadata_json={}); archive=ArchiveObject(sha256=sha,size_bytes=len(content),relative_path=target.relative_to(root).as_posix(),media_type='application/octet-stream',integrity_status='verified',metadata_json={}); db.add_all([account,archive]); db.flush(); sync_manifest_from_archive_object(root, archive); job=ImportJob(source_account_id=account.id,archive_object_id=archive.id,parser_name='fixture',parser_version='0.1',status='pending',report_json={}); db.add(job); db.commit(); job_id=job.id
    assert WorkerRuntime(root,poll_interval=.01,heartbeat_interval=.05,lease_duration=5).run_once() is True
    with Session(engine) as db:
        completed=db.get(ImportJob,job_id); assert completed.status=='completed' and completed.result_code=='imported'; assert db.scalar(select(Conversation).where(Conversation.external_conversation_id=='runtime-c')) is not None


def test_worker_does_not_claim_until_manifest_exists(tmp_path: Path):
    """A committed job is not eligible until its manifest file exists.

    An older job that is still waiting on finalize must not block a later job
    whose manifest is already present.
    """
    root = tmp_path / "data"
    waiting_id = None
    ready_id = None
    try:
        with Session(engine) as db:
            _clear(db)
            waiting_id = _pending_without_manifest(
                db,
                root,
                _zip("waiting-job"),
                "waiting",
                created_at=datetime.now(UTC) - timedelta(minutes=5),
            )
            db.commit()
        assert WorkerRuntime(root, poll_interval=0.01, heartbeat_interval=0.05, lease_duration=5).run_once() is False
        with Session(engine) as db:
            waiting_job = db.get(ImportJob, waiting_id)
            assert waiting_job.status == "pending" and waiting_job.attempt_count == 0
            ready_id = _pending_without_manifest(
                db,
                root,
                _zip("ready-job"),
                "ready",
                created_at=datetime.now(UTC),
            )
            sync_manifest_from_archive_object(
                root, db.get(ArchiveObject, db.get(ImportJob, ready_id).archive_object_id)
            )
            db.commit()
        assert WorkerRuntime(root, poll_interval=0.01, heartbeat_interval=0.05, lease_duration=5).run_once() is True
        with Session(engine) as db:
            waiting_job = db.get(ImportJob, waiting_id)
            ready_job = db.get(ImportJob, ready_id)
            assert waiting_job.status == "pending" and waiting_job.attempt_count == 0
            assert ready_job.status == "completed"
    finally:
        with Session(engine) as db:
            for job_id in (waiting_id, ready_id):
                if job_id is None:
                    continue
                job = db.get(ImportJob, job_id)
                if job is not None and job.status == "pending":
                    job.status = "cancelled"
            db.commit()


def test_startup_repair_runs_once_before_polling(tmp_path, monkeypatch):
    calls = []

    def fake_repair(root, records):
        calls.append(root)
        return (0, len(list(records)))

    monkeypatch.setattr("app.archive.manifest.repair_missing_manifests", fake_repair)
    idle = WorkerRuntime(tmp_path)
    idle._repair_manifests_once()
    assert calls == []

    runtime = WorkerRuntime(tmp_path, repair_manifests_on_start=True)
    runtime.stop_event.set()
    runtime.run_forever()
    runtime.run_forever()
    assert calls == [tmp_path]


def _zip(conversation_id: str) -> bytes:
    payload = {"schema_marker": "qingyu-fixture-v1", "conversations": [{"id": conversation_id, "mapping": {}}]}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("fixture.json", json.dumps(payload))
    return buffer.getvalue()


def _pending_without_manifest(db, root: Path, content: bytes, label: str, created_at=None):
    digest = hashlib.sha256(content).hexdigest()
    target = root / "archive" / "objects" / digest[:2] / digest
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    account = SourceAccount(platform="manifest-gate", account_label=label, metadata_json={})
    archive = ArchiveObject(
        sha256=digest,
        size_bytes=len(content),
        relative_path=target.relative_to(root).as_posix(),
        media_type="application/zip",
        integrity_status="verified",
        metadata_json={},
    )
    db.add_all([account, archive])
    db.flush()
    job = ImportJob(
        source_account_id=account.id,
        archive_object_id=archive.id,
        parser_name="fixture",
        parser_version="0.1",
        status="pending",
        report_json={},
    )
    if created_at is not None:
        job.created_at = created_at
    db.add(job)
    db.flush()
    return job.id
