import dataclasses
import hashlib
import io
import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
import zipfile

import pytest
from sqlalchemy import delete, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db import engine
from app.importers.worker import ClaimedJob, ImportWorker
from app.importers.worker_runtime import WorkerRuntime
from app.models import ArchiveObject, Attachment, Conversation, ImportJob, Message, MessageObservation, MessageRevision, SourceAccount


MARKER = 'SUPER_SECRET_CHAT_CONTENT_123'
TOKEN_MARKER = 'SUPER_SECRET_TOKEN_456'


def _clear(db):
    for model in (Attachment,MessageObservation,MessageRevision,Message,Conversation,ImportJob,SourceAccount,ArchiveObject):
        db.execute(delete(model))
    db.commit()


def _zip_bytes(conversation_id):
    data=io.BytesIO()
    payload={'schema_marker':'qingyu-fixture-v1','conversations':[{'id':conversation_id,'mapping':{}}]}
    with zipfile.ZipFile(data,'w') as archive:
        archive.writestr('fixture.json',json.dumps(payload))
    return data.getvalue()


def _pending_job(root, conversation_id):
    content=_zip_bytes(conversation_id); digest=hashlib.sha256(content).hexdigest()
    target=root/'archive'/'objects'/digest[:2]/digest; target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(content)
    with Session(engine) as db:
        account=SourceAccount(platform='claim-snapshot',account_label=str(uuid.uuid4()),metadata_json={})
        archive=ArchiveObject(sha256=digest,size_bytes=len(content),relative_path=target.relative_to(root).as_posix(),media_type='application/zip',integrity_status='verified',metadata_json={})
        db.add_all([account,archive]); db.flush()
        job=ImportJob(source_account_id=account.id,archive_object_id=archive.id,parser_name='fixture',parser_version='0.1',status='pending',report_json={})
        db.add(job); db.commit(); return job.id


def _real_postgres_operational_error():
    with engine.connect() as connection:
        transaction=connection.begin()
        try:
            connection.execute(text("SET LOCAL statement_timeout = '20ms'"))
            with pytest.raises(OperationalError) as caught:
                connection.execute(text(f"SELECT pg_sleep(1) /* {MARKER} */"))
            return caught.value
        finally:
            transaction.rollback()


def test_claim_returns_frozen_snapshot_and_scalar_access_issues_no_sql():
    statements=[]
    def capture(*args): statements.append(args[2])
    event.listen(engine,'before_cursor_execute',capture)
    try:
        with Session(engine) as db:
            _clear(db)
            account=SourceAccount(platform='snapshot-sql',account_label=str(uuid.uuid4()),metadata_json={})
            archive=ArchiveObject(sha256=uuid.uuid4().hex*2,size_bytes=1,relative_path='x',media_type='x',integrity_status='verified',metadata_json={})
            db.add_all([account,archive]); db.flush()
            job=ImportJob(source_account_id=account.id,archive_object_id=archive.id,parser_name='fixture',parser_version='1',status='pending',report_json={})
            db.add(job); db.commit()
            claim=ImportWorker(db).claim_next(60)
            after_claim=len(statements)
            assert isinstance(claim,ClaimedJob)
            assert claim.job_id and claim.lease_token and claim.source_account_id and claim.archive_object_id and claim.attempt_count==1
            assert len(statements)==after_claim
            with pytest.raises(dataclasses.FrozenInstanceError):
                claim.lease_token=uuid.uuid4()
    finally:
        event.remove(engine,'before_cursor_execute',capture)


def test_post_claim_operational_error_isolated_and_new_session_continues(tmp_path,monkeypatch,caplog,capsys):
    root=tmp_path/'runtime'; error=_real_postgres_operational_error()
    with Session(engine) as db: _clear(db)
    first_id=_pending_job(root,'claimed-then-setup-fails'); time.sleep(.01); second_id=_pending_job(root,'next-job-completes')
    calls={'count':0}; original=WorkerRuntime._prepare_claim
    def fail_once(claim):
        calls['count']+=1
        if calls['count']==1: raise error
        return original(claim)
    monkeypatch.setattr(WorkerRuntime,'_prepare_claim',staticmethod(fail_once))
    sessions=[]
    class TrackingSession(Session):
        def close(self):
            self.was_closed=True
            super().close()
    def session_factory():
        session=TrackingSession(engine); session.was_closed=False; sessions.append(session); return session
    caplog.set_level(logging.ERROR)
    runtime=WorkerRuntime(root,poll_interval=.01,heartbeat_interval=.02,lease_duration=5,error_backoff_max=.02,session_factory=session_factory)
    thread=threading.Thread(target=runtime.run_forever); thread.start()
    deadline=time.time()+3; second_status=None
    while time.time()<deadline:
        with Session(engine) as db: second_status=db.get(ImportJob,second_id).status
        if second_status=='completed': break
        time.sleep(.01)
    runtime.stop_event.set(); thread.join(timeout=1)
    captured=capsys.readouterr(); output=caplog.text+captured.out+captured.err
    with Session(engine) as db:
        assert db.get(ImportJob,first_id).status=='running'
        assert db.get(ImportJob,first_id).result_code is None
    assert second_status=='completed' and not thread.is_alive()
    assert len(sessions)>=2 and sessions[0].was_closed and sessions[1].was_closed
    assert MARKER not in output and 'Traceback' not in output and 'Exception in thread' not in output


def test_finalization_database_error_is_runtime_failure_not_business_failure(tmp_path,monkeypatch,caplog,capsys):
    root=tmp_path/'finalization'; error=_real_postgres_operational_error()
    with Session(engine) as db: _clear(db)
    job_id=_pending_job(root,'finalization-timeout')
    monkeypatch.setattr(ImportWorker,'mark_completed',lambda *args,**kwargs: (_ for _ in ()).throw(error))
    caplog.set_level(logging.ERROR)
    runtime=WorkerRuntime(root,poll_interval=.01,heartbeat_interval=.02,lease_duration=5)
    assert runtime.run_once() is False
    captured=capsys.readouterr(); output=caplog.text+captured.out+captured.err
    with Session(engine) as db:
        job=db.get(ImportJob,job_id)
        assert job.status=='running' and job.result_code is None and job.error_code is None
    assert MARKER not in output and 'Traceback' not in output and 'Exception in thread' not in output


def test_real_main_subprocess_redacts_post_claim_database_failure(tmp_path):
    root=tmp_path/'subprocess'
    with Session(engine) as db: _clear(db)
    first_id=_pending_job(root,'subprocess-first'); time.sleep(.01); second_id=_pending_job(root,'subprocess-second')
    script=f'''\
import threading
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from app.db import engine
from app.importers.worker_runtime import WorkerRuntime, main
marker={MARKER!r}
with engine.connect() as connection:
    transaction=connection.begin()
    try:
        connection.execute(text("SET LOCAL statement_timeout = '20ms'"))
        try: connection.execute(text("SELECT pg_sleep(1) /* " + marker + " */"))
        except OperationalError as caught: injected=caught
    finally: transaction.rollback()
original_prepare=WorkerRuntime._prepare_claim
calls={{'count':0}}
def fail_once(claim):
    calls['count']+=1
    if calls['count']==1: raise injected
    return original_prepare(claim)
WorkerRuntime._prepare_claim=staticmethod(fail_once)
original_forever=WorkerRuntime.run_forever
def bounded(self):
    timer=threading.Timer(1.5,self.stop_event.set); timer.start()
    try: original_forever(self)
    finally: timer.cancel()
WorkerRuntime.run_forever=bounded
main()
'''
    env=os.environ.copy(); env.update({
        'APP_ENV':'test','API_TOKEN':TOKEN_MARKER,'ARCHIVE_ROOT':str(root),
        'IMPORT_WORKER_POLL_SECONDS':'0.01','IMPORT_WORKER_HEARTBEAT_SECONDS':'0.02','IMPORT_WORKER_LEASE_SECONDS':'5',
    })
    completed=subprocess.run([sys.executable,'-c',script],cwd=os.getcwd(),env=env,capture_output=True,text=True,timeout=5)
    output=completed.stdout+completed.stderr
    assert completed.returncode==0
    assert MARKER not in output and TOKEN_MARKER not in output
    assert 'Traceback' not in output and 'Exception in thread' not in output
    with Session(engine) as db:
        assert db.get(ImportJob,first_id).status=='running'
        assert db.get(ImportJob,first_id).result_code is None
        assert db.get(ImportJob,second_id).status=='completed'
