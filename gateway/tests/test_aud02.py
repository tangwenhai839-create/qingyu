import uuid

from app.db import engine
from app.importers.service import ImportService
from app.models import (
    ArchiveObject,
    Conversation,
    ImportJob,
    Message,
    MessageObservation,
    MessageRevision,
    SourceAccount,
)
from sqlalchemy import delete
from sqlalchemy.orm import Session


def _payload(ids):
    return {"schema_marker":"qingyu-fixture-v1","conversations":[{"id": i, "title":"t", "mapping":{}, "current_node":None} for i in ids]}

def test_aud02_existing_job_uses_job_account_and_no_default():
    with Session(engine) as db:
        a=SourceAccount(platform='chatgpt',account_label='account-A',metadata_json={}); ar=ArchiveObject(sha256=uuid.uuid4().hex*2,size_bytes=1,relative_path='x',media_type='application/json',integrity_status='verified',metadata_json={}); db.add_all([a,ar]); db.flush(); j=ImportJob(source_account_id=a.id,archive_object_id=ar.id,parser_name='p',parser_version='1',status='running',lease_token=uuid.uuid4(),lease_expires_at=__import__('datetime').datetime.now(__import__('datetime').timezone.utc)+__import__('datetime').timedelta(hours=1),report_json={}); db.add(j); db.commit()
        before=db.query(SourceAccount).filter_by(platform='fixture',account_label='default').count()
        ImportService(db).import_fixture(_payload(['same']), ar.id, job=j, lease_token=j.lease_token)
        c=db.query(Conversation).filter_by(external_conversation_id='same').one(); assert c.source_account_id==a.id; assert db.query(SourceAccount).filter_by(platform='fixture',account_label='default').count()==before

def test_aud02_missing_ids_are_distinct_and_deterministic():
    with Session(engine) as db:
        for m in (MessageObservation,MessageRevision,Message,Conversation,ImportJob,SourceAccount,ArchiveObject): db.execute(delete(m))
        a=SourceAccount(platform='fixture',account_label='x',metadata_json={}); ar=ArchiveObject(sha256='c'*64,size_bytes=1,relative_path='x',media_type='application/json',integrity_status='verified',metadata_json={}); db.add_all([a,ar]); db.flush(); j=ImportJob(source_account_id=a.id,archive_object_id=ar.id,parser_name='p',parser_version='1',status='running',lease_token=uuid.uuid4(),lease_expires_at=__import__('datetime').datetime.now(__import__('datetime').timezone.utc)+__import__('datetime').timedelta(hours=1),report_json={}); db.add(j); db.commit()
        p={"schema_marker":"qingyu-fixture-v1","conversations":[{"title":"a","mapping":{}},{"title":"b","mapping":{}}]}
        ImportService(db).import_fixture(p, ar.id, job=j, lease_token=j.lease_token); vals=[c.external_conversation_id for c in db.query(Conversation).all()]; assert len(vals)==2 and len(set(vals))==2 and all(v.startswith('synthetic:') for v in vals)




