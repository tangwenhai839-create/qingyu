import uuid

import pytest
from app.db import engine
from app.importers.service import ImportService
from app.models import (
    ArchiveObject,
    Attachment,
    Conversation,
    ConversationObservation,
    ImportJob,
    MappingNodeObservation,
    Message,
    MessageObservation,
    MessageRevision,
    SourceAccount,
)
from sqlalchemy import delete
from sqlalchemy.orm import Session


@pytest.fixture()
def import_db():
    with Session(engine) as db:
        for model in (MappingNodeObservation, ConversationObservation, Attachment, MessageObservation, MessageRevision, Message, Conversation, ImportJob, SourceAccount, ArchiveObject):
            db.execute(delete(model))
        archive = ArchiveObject(sha256=uuid.uuid4().hex * 2, size_bytes=1, relative_path="archive/objects/test", media_type="application/json", integrity_status="verified", metadata_json={})
        account = SourceAccount(platform="fixture", account_label=str(uuid.uuid4()), metadata_json={})
        db.add_all([archive, account])
        db.flush()
        job = ImportJob(source_account_id=account.id, archive_object_id=archive.id, parser_name="fixture", parser_version="0.1", status="pending", report_json={})
        db.add(job); db.commit()
        yield db, archive.id
        db.rollback()

def test_import_service_persists_tree(import_db):
    db, archive_id = import_db
    payload = {"schema_marker":"qingyu-fixture-v1", "conversations": [{"id": "c1", "title": "Fixture", "current_node": "n2", "mapping": {"n1": {"parent": None, "message": {"author": {"role": "user"}, "content": {"parts": ["hi"]}}}, "n2": {"parent": "n1", "message": {"author": {"role": "assistant"}, "content": {"parts": ["hello"]}}}}}]}
    from app.importers.worker import ImportWorker
    job = ImportWorker(db).claim_next(); outcome = ImportService(db).import_fixture(payload, archive_id, job=job, lease_token=job.lease_token)
    assert outcome.report["messages"]["new"] == 2
    assert db.query(Conversation).count() == 1
    assert db.query(Message).count() == 2
    assert db.query(MessageRevision).count() == 2
    assert db.query(MessageObservation).count() == 2
