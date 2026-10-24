import hashlib
import json
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.db import engine
from app.importers.processor import ArchiveConsumptionIntegrityError, ImportProcessor
from app.models import ArchiveObject, Conversation, ImportJob, Message, SourceAccount
from sqlalchemy.orm import Session


def test_zip_processor_fixture(tmp_path: Path):
    root=tmp_path/'data'; (root/'archive/objects/aa').mkdir(parents=True)
    payload={"schema_marker":"qingyu-fixture-v1","conversations":[{"id":"pc1","title":"p","mapping":{"n1":{"parent":None,"message":{"author":{"role":"user"},"content":{"parts":["x"]}}}},"current_node":"n1"}]}
    z=tmp_path/'x.zip'
    with zipfile.ZipFile(z,'w') as f:f.writestr('fixture.json',json.dumps(payload))
    sha=hashlib.sha256(z.read_bytes()).hexdigest(); objpath=root/'archive/objects'/sha[:2]/sha; objpath.parent.mkdir(parents=True, exist_ok=True); objpath.write_bytes(z.read_bytes())
    with Session(engine) as db:
        a=ArchiveObject(sha256=sha,size_bytes=z.stat().st_size,relative_path=f'archive/objects/{sha[:2]}/{sha}',media_type='application/zip',integrity_status='verified',metadata_json={}); s=SourceAccount(platform='proc',account_label=str(uuid.uuid4()),metadata_json={}); db.add_all([a,s]); db.flush(); j=ImportJob(source_account_id=s.id,archive_object_id=a.id,parser_name='generic-tree-fixture',parser_version='0.1',status='running',lease_token=uuid.uuid4(),lease_expires_at=datetime.now(UTC)+timedelta(seconds=60),report_json={}); db.add(j); db.commit(); result=ImportProcessor(db,root).process(j,j.lease_token); assert result['imported_messages']==1


@pytest.mark.parametrize('replacement', [b'x', b'corrupted-different-size'])
def test_processor_rejects_changed_archive_bytes_before_import(tmp_path: Path, replacement: bytes):
    root=tmp_path/'data'; original=tmp_path/'original.zip'
    with zipfile.ZipFile(original,'w') as archive:
        archive.writestr('fixture.json', json.dumps({'schema_marker':'qingyu-fixture-v1','conversations':[{'id':f'blocked-{uuid.uuid4()}','mapping':{}}]}))
    content=original.read_bytes(); sha=hashlib.sha256(content).hexdigest(); target=root/'archive'/'objects'/sha[:2]/sha; target.parent.mkdir(parents=True); target.write_bytes(content)
    if len(replacement) == 1:
        replacement = bytes([content[0] ^ 1]) + content[1:]
    target.write_bytes(replacement)
    with Session(engine) as db:
        account=SourceAccount(platform='aud05',account_label=str(uuid.uuid4()),metadata_json={}); obj=ArchiveObject(sha256=sha,size_bytes=len(content),relative_path=target.relative_to(root).as_posix(),media_type='application/zip',integrity_status='verified',metadata_json={}); db.add_all([account,obj]); db.flush(); job=ImportJob(source_account_id=account.id,archive_object_id=obj.id,parser_name='fixture',parser_version='0.1',status='running',lease_token=uuid.uuid4(),lease_expires_at=datetime.now(UTC)+timedelta(seconds=60),report_json={}); db.add(job); db.commit()
        with pytest.raises(ArchiveConsumptionIntegrityError): ImportProcessor(db,root).process(job,job.lease_token)
        db.expire_all(); assert db.get(ImportJob,job.id).status == 'failed'; assert db.get(ArchiveObject,obj.id).integrity_status == 'integrity_failure'
        assert db.query(Conversation).filter(Conversation.source_account_id==account.id).count()==0; assert db.query(Message).join(Conversation).filter(Conversation.source_account_id==account.id).count()==0
