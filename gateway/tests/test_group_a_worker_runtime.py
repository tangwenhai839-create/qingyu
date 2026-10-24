import hashlib
import json
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
        _clear(db); account=SourceAccount(platform='runtime',account_label='a',metadata_json={}); archive=ArchiveObject(sha256=sha,size_bytes=len(content),relative_path=target.relative_to(root).as_posix(),media_type='application/octet-stream',integrity_status='verified',metadata_json={}); db.add_all([account,archive]); db.flush(); job=ImportJob(source_account_id=account.id,archive_object_id=archive.id,parser_name='fixture',parser_version='0.1',status='pending',report_json={}); db.add(job); db.commit(); job_id=job.id
    assert WorkerRuntime(root,poll_interval=.01,heartbeat_interval=.05,lease_duration=5).run_once() is True
    with Session(engine) as db:
        completed=db.get(ImportJob,job_id); assert completed.status=='completed' and completed.result_code=='imported'; assert db.scalar(select(Conversation).where(Conversation.external_conversation_id=='runtime-c')) is not None
