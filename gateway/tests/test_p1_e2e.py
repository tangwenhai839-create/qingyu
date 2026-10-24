import io
import json
import zipfile
from pathlib import Path

from app.config import get_settings
from app.db import engine
from app.importers.query import ImportedTreeQuery
from app.importers.worker_runtime import WorkerRuntime
from app.main import app
from app.models import (
    ArchiveObject,
    ArchiveObservation,
    Attachment,
    Conversation,
    ImportJob,
    Message,
    MessageObservation,
    MessageRevision,
    SourceAccount,
)
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session


def _clear(db):
    for model in (Attachment,MessageObservation,MessageRevision,Message,Conversation,ArchiveObservation,ImportJob,SourceAccount,ArchiveObject): db.execute(delete(model))
    db.commit()


def test_post_to_worker_to_snapshot_and_report(tmp_path: Path):
    root=tmp_path/'data'; get_settings().archive_root=str(root)
    payload={'schema_marker':'qingyu-fixture-v1','conversations':[{'id':'e2e','current_node':'child','mapping':{'root':{'parent':None,'message':{'author':{'role':'user'},'content':{'parts':['hi']}}},'child':{'parent':'root','message':{'author':{'role':'assistant'},'content':{'parts':['hello']}}}}}]}
    content=io.BytesIO()
    with zipfile.ZipFile(content,'w') as archive: archive.writestr('nested/fixture.json',json.dumps(payload))
    with Session(engine) as db: _clear(db)
    client=TestClient(app); headers={'Authorization':'Bearer qingyu-test-token-only'}
    response=client.post('/api/v1/import/chatgpt',headers=headers,files={'file':('export.zip',content.getvalue(),'application/octet-stream')})
    assert response.status_code==202; job_id=response.json()['import_job_id']
    with Session(engine) as db: assert db.get(ImportJob,job_id).status=='pending'
    assert WorkerRuntime(root,poll_interval=.01,heartbeat_interval=.05,lease_duration=5).run_once()
    result=client.get(f'/api/v1/import-jobs/{job_id}',headers=headers); body=result.json(); assert body['status']=='completed' and body['result_code']=='imported'; assert body['counts']['imported']==2
    with Session(engine) as db:
        job=db.get(ImportJob,job_id); conversation=db.query(Conversation).filter_by(external_conversation_id='e2e').one(); assert db.query(Message).count()==2 and db.query(MessageRevision).count()==2 and db.query(MessageObservation).count()==2
        current=ImportedTreeQuery(db).get_current_path(conversation.id,job.id); assert [row.external_node_id for row in current]==['root','child']; assert all(row.metadata_json['zip_member']=='nested/fixture.json' for row in current)
        archive=db.get(ArchiveObject,job.archive_object_id); assert (root/archive.relative_path).exists(); assert db.query(ArchiveObservation).filter_by(import_job_id=job.id).count()==1
    assert not list((root/'staging'/job_id).glob('*/*'))


def _zip(member, payload):
    content=io.BytesIO()
    with zipfile.ZipFile(content,'w') as archive: archive.writestr(member,json.dumps(payload) if not isinstance(payload,bytes) else payload)
    return content.getvalue()


def _submit(client, headers, content):
    response=client.post('/api/v1/import/chatgpt',headers=headers,files={'file':('export.zip',content,'application/zip')})
    assert response.status_code==202
    return response.json()['import_job_id']


def test_failed_worker_e2e_keeps_raw_archive_and_no_business_rows(tmp_path: Path):
    root=tmp_path/'failed-data'; get_settings().archive_root=str(root); client=TestClient(app); headers={'Authorization':'Bearer qingyu-test-token-only'}
    cases=[
        (_zip('../escape.json',{}),'processor_error'),
        (_zip('unknown.json',{}),'unsupported_structure'),
        (_zip('fixture.json',{'schema_marker':'qingyu-fixture-v1','conversations':[{'id':'bad','mapping':[]}]}),'parser_validation_error'),
    ]
    for content,expected in cases:
        with Session(engine) as db: _clear(db)
        job_id=_submit(client,headers,content); assert WorkerRuntime(root,poll_interval=.01,heartbeat_interval=.05,lease_duration=5).run_once()
        with Session(engine) as db:
            job=db.get(ImportJob,job_id); assert job.status=='failed' and job.result_code==expected; assert db.query(Conversation).count()==0; archive=db.get(ArchiveObject,job.archive_object_id); assert (root/archive.relative_path).exists()


def test_corrupted_archive_failure_e2e(tmp_path: Path):
    root=tmp_path/'corrupt-data'; get_settings().archive_root=str(root); client=TestClient(app); headers={'Authorization':'Bearer qingyu-test-token-only'}
    with Session(engine) as db: _clear(db)
    job_id=_submit(client,headers,_zip('fixture.json',{'schema_marker':'qingyu-fixture-v1','conversations':[]}))
    with Session(engine) as db:
        job=db.get(ImportJob,job_id); archive=db.get(ArchiveObject,job.archive_object_id); target=root/archive.relative_path; target.write_bytes(b'corrupted')
    assert WorkerRuntime(root,poll_interval=.01,heartbeat_interval=.05,lease_duration=5).run_once()
    with Session(engine) as db: job=db.get(ImportJob,job_id); assert job.status=='failed' and job.result_code=='archive_integrity_failure'; assert db.query(Conversation).count()==0
