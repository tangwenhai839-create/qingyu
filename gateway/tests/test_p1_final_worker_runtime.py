import hashlib
import io
import json
import logging
import threading
import time
import uuid
import zipfile
from pathlib import Path

import pytest
from app.db import engine
from app.importers.processor import ImportProcessor
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
from sqlalchemy import delete
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session


def _clear(db):
    for model in (Attachment, MessageObservation, MessageRevision, Message, Conversation, ImportJob, SourceAccount, ArchiveObject):
        db.execute(delete(model))
    db.commit()


def _zip_bytes(valid=True):
    if not valid:
        return b'not-a-zip'
    data = io.BytesIO()
    payload = {'schema_marker':'qingyu-fixture-v1','conversations':[{'id':str(uuid.uuid4()),'mapping':{}}]}
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('fixture.json', json.dumps(payload))
    return data.getvalue()


def _pending_job(root: Path, content=None):
    content = content if content is not None else _zip_bytes()
    digest = hashlib.sha256(content).hexdigest()
    target = root / 'archive' / 'objects' / digest[:2] / digest
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    with Session(engine) as db:
        account = SourceAccount(platform='runtime-final', account_label=str(uuid.uuid4()), metadata_json={})
        archive = ArchiveObject(sha256=digest,size_bytes=len(content),relative_path=target.relative_to(root).as_posix(),media_type='application/zip',integrity_status='verified',metadata_json={})
        db.add_all([account,archive]); db.flush()
        job = ImportJob(source_account_id=account.id,archive_object_id=archive.id,parser_name='fixture',parser_version='0.1',status='pending',report_json={})
        db.add(job); db.commit()
        return job.id


@pytest.mark.parametrize('failing_operation', ['recover_stale', 'claim_next'])
def test_transient_recover_or_claim_error_does_not_stop_runtime(tmp_path, monkeypatch, caplog, capsys, failing_operation):
    marker = 'SUPER_SECRET_CHAT_CONTENT_123'
    root = tmp_path / failing_operation
    with Session(engine) as db:
        _clear(db)
    job_id = _pending_job(root)
    original = getattr(ImportWorker, failing_operation)
    calls = {'count': 0}

    def flaky(self, *args, **kwargs):
        calls['count'] += 1
        if calls['count'] == 1:
            raise OperationalError('safe statement', {'value': marker}, Exception(marker))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ImportWorker, failing_operation, flaky)
    caplog.set_level(logging.ERROR)
    runtime = WorkerRuntime(root,poll_interval=.01,heartbeat_interval=.02,lease_duration=5,error_backoff_max=.02)
    thread = threading.Thread(target=runtime.run_forever)
    thread.start()
    deadline = time.time() + 3
    status = None
    while time.time() < deadline:
        with Session(engine) as db:
            status = db.get(ImportJob, job_id).status
        if status == 'completed':
            break
        time.sleep(.01)
    runtime.stop_event.set(); thread.join(timeout=1)
    captured = capsys.readouterr()
    assert status == 'completed' and not thread.is_alive() and calls['count'] >= 2
    assert marker not in caplog.text + captured.out + captured.err


def test_failed_job_does_not_prevent_processing_next_job(tmp_path):
    root = tmp_path / 'continue'
    with Session(engine) as db:
        _clear(db)
    failed_id = _pending_job(root, _zip_bytes(valid=False))
    time.sleep(.01)
    completed_id = _pending_job(root)
    runtime = WorkerRuntime(root,poll_interval=.01,heartbeat_interval=.02,lease_duration=5)
    assert runtime.run_once() is True
    assert runtime.run_once() is True
    with Session(engine) as db:
        assert db.get(ImportJob, failed_id).status == 'failed'
        assert db.get(ImportJob, completed_id).status == 'completed'


def test_graceful_stop_exits_idle_runtime(tmp_path):
    with Session(engine) as db:
        _clear(db)
    runtime = WorkerRuntime(tmp_path,poll_interval=.01)
    thread = threading.Thread(target=runtime.run_forever)
    thread.start(); time.sleep(.03); runtime.stop_event.set(); thread.join(timeout=1)
    assert not thread.is_alive()


def test_heartbeat_exception_is_signalled_without_thread_traceback(tmp_path, monkeypatch, caplog, capsys):
    marker = 'SUPER_SECRET_CHAT_CONTENT_123'
    root = tmp_path / 'heartbeat'
    with Session(engine) as db:
        _clear(db)
    job_id = _pending_job(root)

    def broken_heartbeat(self, *args, **kwargs):
        raise OperationalError('safe statement', {'value': marker}, Exception(marker))

    def wait_for_health_failure(self, job, token, health_check=None):
        deadline = time.time() + 1
        while time.time() < deadline:
            time.sleep(.01)
            health_check()
        raise AssertionError('heartbeat failure was not signalled')

    monkeypatch.setattr(ImportWorker, 'heartbeat', broken_heartbeat)
    monkeypatch.setattr(ImportProcessor, 'process', wait_for_health_failure)
    caplog.set_level(logging.INFO)
    assert WorkerRuntime(root,poll_interval=.01,heartbeat_interval=.01,lease_duration=5).run_once() is True
    captured = capsys.readouterr()
    output = caplog.text + captured.out + captured.err
    assert marker not in output
    assert 'Exception in thread' not in output and 'Traceback' not in output
    with Session(engine) as db:
        assert db.get(ImportJob, job_id).status == 'running'
