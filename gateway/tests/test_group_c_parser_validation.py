import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db import engine
from app.importers.generic import (
    GenericTreeParser,
    ParserRegistry,
    ParserValidationError,
)
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


def test_top_level_array_is_structured_unsupported():
    assert ParserRegistry().detect([]) == ('unsupported_structure', None)


def test_full_validation_reports_unknown_fields_and_bad_later_conversation():
    parser=GenericTreeParser(); payload={'schema_marker':'qingyu-fixture-v1','extra':1,'conversations':[{'id':'valid','mapping':{}},{'id':'bad','mapping':[]} ]}
    with pytest.raises(ParserValidationError) as caught: parser.validate(payload,'fixture.json')
    assert caught.value.errors[0]['raw_pointer']=='/conversations/1/mapping'
    unknown_payload={'schema_marker':'qingyu-fixture-v1','extra':1,'conversations':[]}
    assert parser.validate(unknown_payload,'fixture.json')[0] == {'type':'unknown_field','severity':'warning','file':'fixture.json','field':'extra','raw_pointer':'/extra'}


def test_invalid_later_conversation_persists_nothing():
    with Session(engine) as db:
        for model in (Attachment,MessageObservation,MessageRevision,Message,Conversation,ImportJob,SourceAccount,ArchiveObject): db.execute(delete(model))
        account=SourceAccount(platform='parser',account_label='stable',metadata_json={}); archive=ArchiveObject(sha256='5'*64,size_bytes=1,relative_path='x',media_type='x',integrity_status='verified',metadata_json={}); db.add_all([account,archive]); db.flush(); job=ImportJob(source_account_id=account.id,archive_object_id=archive.id,parser_name='fixture',parser_version='1',status='running',lease_token=uuid.uuid4(),lease_expires_at=datetime.now(UTC)+timedelta(minutes=1),report_json={}); db.add(job); db.commit()
        payload={'schema_marker':'qingyu-fixture-v1','conversations':[{'id':'valid','mapping':{}},{'id':'bad','mapping':[]}]}
        with pytest.raises(ParserValidationError): ImportService(db).import_fixture(payload,archive.id,job=job,lease_token=job.lease_token,parser=GenericTreeParser())
        assert db.query(Conversation).count()==0


def _valid_conversation(conversation_id):
    return {'id':conversation_id,'title':'valid','current_node':'n1','metadata':{},'mapping':{'n1':{'id':'n1','parent':None,'children':[],'metadata':{},'message':{'id':'m1','author':{'role':'user','metadata':{}},'content':{'content_type':'text','parts':['hello'],'metadata':{}},'metadata':{'model_slug':'fixture-model'},'model':'fixture-model','create_time':1.0,'update_time':2.0,'attachments':[]}}}}


@pytest.mark.parametrize('invalid_value', [['invalid'], 'invalid'])
def test_message_metadata_type_is_rejected_before_any_persistence(invalid_value):
    payload={'schema_marker':'qingyu-fixture-v1','conversations':[_valid_conversation('valid'),_valid_conversation('bad')]}
    payload['conversations'][1]['mapping']['n1']['message']['metadata']=invalid_value
    _assert_payload_rejected_without_business_rows(payload, '/conversations/1/mapping/n1/message/metadata')


@pytest.mark.parametrize(('field','value','pointer'), [
    ('message', [], '/conversations/1/mapping/n1/message'),
    ('author', [], '/conversations/1/mapping/n1/message/author'),
    ('content', [], '/conversations/1/mapping/n1/message/content'),
])
def test_nested_message_contract_types_are_rejected_before_persistence(field, value, pointer):
    payload={'schema_marker':'qingyu-fixture-v1','conversations':[_valid_conversation('valid'),_valid_conversation('bad')]}
    node=payload['conversations'][1]['mapping']['n1']
    if field=='message': node['message']=value
    else: node['message'][field]=value
    _assert_payload_rejected_without_business_rows(payload, pointer)


def test_valid_complete_fixture_contract_imports_normally():
    with Session(engine) as db:
        job, archive = _fresh_job(db)
        outcome=ImportService(db).import_fixture({'schema_marker':'qingyu-fixture-v1','conversations':[_valid_conversation('valid')]},archive.id,job=job,lease_token=job.lease_token,parser=GenericTreeParser())
        assert outcome.report['conversations']['new']==1
        assert db.query(Conversation).count()==1 and db.query(Message).count()==1


def _assert_payload_rejected_without_business_rows(payload, pointer):
    with Session(engine) as db:
        job, archive = _fresh_job(db)
        with pytest.raises(ParserValidationError) as caught:
            ImportService(db).import_fixture(payload,archive.id,job=job,lease_token=job.lease_token,parser=GenericTreeParser())
        assert any(error['raw_pointer']==pointer for error in caught.value.errors)
        assert db.query(Conversation).count()==0
        assert db.query(Message).count()==0
        assert db.query(MessageRevision).count()==0
        assert db.query(MessageObservation).count()==0


def _fresh_job(db):
    for model in (Attachment,MessageObservation,MessageRevision,Message,Conversation,ImportJob,SourceAccount,ArchiveObject): db.execute(delete(model))
    account=SourceAccount(platform='parser',account_label=str(uuid.uuid4()),metadata_json={})
    archive=ArchiveObject(sha256=uuid.uuid4().hex*2,size_bytes=1,relative_path='x',media_type='x',integrity_status='verified',metadata_json={})
    db.add_all([account,archive]); db.flush()
    job=ImportJob(source_account_id=account.id,archive_object_id=archive.id,parser_name='fixture',parser_version='1',status='running',lease_token=uuid.uuid4(),lease_expires_at=datetime.now(UTC)+timedelta(minutes=1),report_json={})
    db.add(job); db.commit()
    return job, archive
