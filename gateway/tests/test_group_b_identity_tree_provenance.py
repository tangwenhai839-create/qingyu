import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from app.db import engine
from app.importers.query import ImportedTreeQuery
from app.importers.service import ImportService
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
from sqlalchemy.orm import Session


def _clear(db):
    for model in (Attachment, MessageObservation, MessageRevision, Message, Conversation, ImportJob, SourceAccount, ArchiveObject): db.execute(delete(model))
    db.commit()


def test_tree_queries_never_mix_import_snapshots():
    with Session(engine) as db:
        _clear(db); account=SourceAccount(platform='tree',account_label='stable',metadata_json={}); archive=ArchiveObject(sha256='2'*64,size_bytes=1,relative_path='x',media_type='x',integrity_status='verified',metadata_json={}); db.add_all([account,archive]); db.flush(); conversation=Conversation(source_account_id=account.id,external_conversation_id='tree',metadata_json={}); db.add(conversation); db.flush()
        jobs=[ImportJob(source_account_id=account.id,archive_object_id=archive.id,parser_name='p',parser_version='1',status='completed',report_json={}) for _ in range(2)]; db.add_all(jobs); db.flush()
        messages={name:Message(conversation_id=conversation.id,external_message_id=name) for name in ('A','B','C1','C2','D1','D2')}; db.add_all(messages.values()); db.flush()
        revisions={name:MessageRevision(message_id=msg.id,revision_no=1,role='user',content_text=name,content_json={},revision_fingerprint=uuid.uuid4().hex*2) for name,msg in messages.items()}; db.add_all(revisions.values()); db.flush()
        def observe(job, node, parent, order, current=False): db.add(MessageObservation(import_job_id=job.id,message_id=messages[node].id,message_revision_id=revisions[node].id,external_node_id=node,external_parent_node_id=parent,parent_message_id=messages[parent].id if parent else None,is_current_path=current,observation_order=order,metadata_json={}))
        for order,(node,parent) in enumerate((('A',None),('B','A'),('C1','B'),('D1','C1'))): observe(jobs[0],node,parent,order,node=='D1')
        for order,(node,parent) in enumerate((('A',None),('C2','A'),('B','C2'),('D2','B'))): observe(jobs[1],node,parent,order,node=='D2')
        db.commit(); query=ImportedTreeQuery(db)
        assert [row.external_node_id for row in query.get_path_to_leaf(conversation.id,jobs[0].id,messages['D1'].id)] == ['A','B','C1','D1']
        assert [row.external_node_id for row in query.get_current_path(conversation.id,jobs[1].id)] == ['A','C2','B','D2']


def test_raw_pointer_escapes_node_and_resolves_source_json():
    payload={'schema_marker':'qingyu-fixture-v1','conversations':[{'id':'pointer','mapping':{'a/b~c':{'parent':None,'message':{'author':{'role':'user'},'content':{'parts':['x']}}}}}]}
    with Session(engine) as db:
        _clear(db); account=SourceAccount(platform='pointer',account_label='stable',metadata_json={}); archive=ArchiveObject(sha256='3'*64,size_bytes=1,relative_path='x',media_type='x',integrity_status='verified',metadata_json={}); db.add_all([account,archive]); db.flush(); job=ImportJob(source_account_id=account.id,archive_object_id=archive.id,parser_name='p',parser_version='1',status='running',lease_token=uuid.uuid4(),lease_expires_at=datetime.now(UTC)+timedelta(minutes=1),report_json={}); db.add(job); db.commit()
        ImportService(db).import_fixture(payload,archive.id,job=job,lease_token=job.lease_token,source_member='nested/fixture.json')
        observation=db.query(MessageObservation).one(); assert observation.raw_mapping_pointer=='/conversations/0/mapping/a~1b~0c'; assert observation.metadata_json['zip_member']=='nested/fixture.json'; assert payload['conversations'][0]['mapping']['a/b~c']['message']['content']['parts']==['x']


def test_concurrent_jobs_reuse_one_stable_message():
    payload={'schema_marker':'qingyu-fixture-v1','conversations':[{'id':'concurrent','mapping':{'node':{'parent':None,'message':{'author':{'role':'user'},'content':{'parts':['same']}}}}}]}
    with Session(engine) as db:
        _clear(db); account=SourceAccount(platform='concurrent',account_label='stable',metadata_json={}); archive=ArchiveObject(sha256='4'*64,size_bytes=1,relative_path='x',media_type='x',integrity_status='verified',metadata_json={}); db.add_all([account,archive]); db.flush(); jobs=[]
        for _ in range(2):
            job=ImportJob(source_account_id=account.id,archive_object_id=archive.id,parser_name='p',parser_version='1',status='running',lease_token=uuid.uuid4(),lease_expires_at=datetime.now(UTC)+timedelta(minutes=1),report_json={}); db.add(job); jobs.append(job)
        db.commit(); specs=[(job.id,job.lease_token,archive.id) for job in jobs]
    def run(spec):
        with Session(engine) as db:
            job=db.get(ImportJob,spec[0]); ImportService(db).import_fixture(payload,spec[2],job=job,lease_token=spec[1])
    with ThreadPoolExecutor(max_workers=2) as pool: list(pool.map(run,specs))
    with Session(engine) as db:
        conversation=db.query(Conversation).filter_by(external_conversation_id='concurrent').one(); assert db.query(Message).filter_by(conversation_id=conversation.id,external_message_id='node').count()==1; assert db.query(MessageRevision).count()==1; assert db.query(MessageObservation).count()==2
