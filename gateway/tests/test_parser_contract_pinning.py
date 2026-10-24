import hashlib
import json
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from app import api_import
from app.importers import processor as processor_module
from app.importers.generic import GenericTreeParser, ParserRegistry
from app.importers.processor import ImportProcessor
from app.importers.report import empty_report
from app.importers.retry_policy import (
    PARSER_CONTRACT_CONFLICT,
    PARSER_CONTRACT_UNAVAILABLE,
    resolve_durable_parser_contract,
)
from app.importers.service import ImportService
from app.importers.worker import ImportWorker
from app.main import app
from app.models import (
    ArchiveObject,
    ArchiveObservation,
    Attachment,
    AttachmentObservation,
    Conversation,
    ImportBatchCommit,
    ImportJob,
    Message,
    MessageObservation,
    MessageRevision,
    SourceAccount,
)
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

DB_URL = __import__("os").environ["TEST_DATABASE_URL"]
HEADERS = {"Authorization": "Bearer qingyu-test-token-only"}


@pytest.fixture()
def pinning_db():
    engine = create_engine(DB_URL)
    with Session(engine, expire_on_commit=False) as db:
        models = (
            AttachmentObservation, Attachment, MessageObservation, MessageRevision,
            Message, ImportBatchCommit, Conversation, ArchiveObservation, ImportJob,
            SourceAccount, ArchiveObject,
        )
        for model in models:
            db.execute(delete(model))
        db.commit()
        try:
            yield db
        finally:
            db.rollback()
            for model in models:
                db.execute(delete(model))
            db.commit()
    engine.dispose()


def _durable_job(db, identities, *, status="partial"):
    account = SourceAccount(platform="pinning", account_label=str(uuid.uuid4()), metadata_json={})
    archive = ArchiveObject(sha256=uuid.uuid4().hex * 2, size_bytes=1, relative_path="unused", media_type="x", integrity_status="verified", metadata_json={})
    db.add_all([account, archive]); db.flush()
    job = ImportJob(source_account_id=account.id, archive_object_id=archive.id, parser_name="versioned-fixture", parser_version="1", status=status, attempt_count=1, report_json={})
    db.add(job); db.flush()
    for sequence, identity in enumerate(identities, 1):
        conversation = Conversation(source_account_id=account.id, external_conversation_id=f"c-{sequence}", metadata_json={})
        db.add(conversation); db.flush()
        db.add(ImportBatchCommit(import_job_id=job.id, conversation_id=conversation.id, sequence_no=sequence, batch_key=f"b-{sequence}", external_conversation_id=conversation.external_conversation_id, parser_name=identity[0], contract_version=identity[1], report_delta=empty_report(*identity, "matched"), counts_delta={"discovered_count": 0, "imported_count": 0, "skipped_count": 0, "failed_count": 0}))
    db.commit()
    return job


class ParserV1(GenericTreeParser):
    name = "versioned-fixture"
    version = "1"


class ParserV2(GenericTreeParser):
    name = "versioned-fixture"
    version = "2"


def _job(version="1", status="partial"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        parser_name="versioned-fixture",
        parser_version=version,
        archive_object_id=uuid.uuid4(),
        source_account_id=uuid.uuid4(),
    )


def test_a_registry_resolves_only_an_exact_unique_contract():
    v1, v2 = ParserV1(), ParserV2()
    registry = ParserRegistry([v2, v1])
    assert registry.resolve("versioned-fixture", "1") is v1
    assert registry.resolve("versioned-fixture", "2") is v2
    assert registry.resolve("versioned-fixture", "3") is None
    assert ParserRegistry([v1, ParserV1()]).resolve("versioned-fixture", "1") is None
    assert ParserRegistry([]).resolve("versioned-fixture", "1") is None


class _Rows:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class _RetryDb:
    def __init__(self, job, identities):
        self.job = job
        self.identities = identities
        self.rollbacks = 0

    def get(self, _model, _identifier):
        return self.job

    def execute(self, _statement):
        return _Rows(self.identities)

    def rollback(self):
        self.rollbacks += 1


def _install_api_registry(monkeypatch, parsers):
    monkeypatch.setattr(api_import, "ParserRegistry", lambda: ParserRegistry(parsers))


def test_b_v1_partial_retries_with_v1_when_current_v2_and_v1_are_available(monkeypatch):
    job = _job("1")
    db = _RetryDb(job, [("versioned-fixture", "1")])
    calls = []

    class Worker:
        def __init__(self, _db):
            pass

        def retry(self, job_id, expected_parser_name=None, expected_parser_version=None):
            calls.append((job_id, expected_parser_name, expected_parser_version))
            return True

    _install_api_registry(monkeypatch, [ParserV2(), ParserV1()])
    monkeypatch.setattr(api_import, "ImportWorker", Worker)

    assert api_import.retry_import_job(job.id, db) == {"import_job_id": str(job.id), "status": "pending"}
    assert calls == [(job.id, "versioned-fixture", "1")]


def test_c_v1_partial_stays_terminal_when_v1_is_unavailable(monkeypatch):
    job = _job("1")
    db = _RetryDb(job, [("versioned-fixture", "1")])
    _install_api_registry(monkeypatch, [ParserV2()])
    monkeypatch.setattr(api_import, "ImportWorker", lambda _db: pytest.fail("retry must not run"))

    with pytest.raises(HTTPException) as caught:
        api_import.retry_import_job(job.id, db)

    assert caught.value.status_code == 409
    assert caught.value.detail == PARSER_CONTRACT_UNAVAILABLE
    assert job.status == "partial"


@pytest.mark.parametrize(
    "identities",
    [
        [("versioned-fixture", "1"), ("versioned-fixture", "2")],
        [("versioned-fixture", "2")],
    ],
)
def test_d_conflicting_or_job_mismatched_ledger_fails_before_pending(monkeypatch, identities):
    job = _job("1")
    db = _RetryDb(job, identities)
    _install_api_registry(monkeypatch, [ParserV1(), ParserV2()])
    monkeypatch.setattr(api_import, "ImportWorker", lambda _db: pytest.fail("retry must not run"))

    with pytest.raises(HTTPException) as caught:
        api_import.retry_import_job(job.id, db)

    assert caught.value.status_code == 409
    assert caught.value.detail == PARSER_CONTRACT_CONFLICT
    assert job.status == "partial"


def test_e_processor_bypass_executes_pinned_v1_without_detecting_v2(monkeypatch, tmp_path: Path):
    payload = {"schema_marker": "qingyu-fixture-v1", "conversations": []}
    root = tmp_path / "data"
    source = tmp_path / "input.zip"
    with zipfile.ZipFile(source, "w") as bundle:
        bundle.writestr("fixture.json", json.dumps(payload))
    content = source.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    relative = f"archive/objects/{digest[:2]}/{digest}"
    target = root / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    job = _job("1", "running")
    archive = SimpleNamespace(
        id=job.archive_object_id,
        integrity_status="verified",
        relative_path=relative,
        size_bytes=len(content),
        sha256=digest,
    )
    account = SimpleNamespace(platform="fixture")
    executions = []

    class V1(ParserV1):
        def parse(self, value):
            executions.append("v1")
            return super().parse(value)

    class V2(ParserV2):
        def can_parse(self, _value):
            raise AssertionError("pinned replay must not run detection")

        def parse(self, _value):
            raise AssertionError("v2 must not execute")

    v1 = V1()

    class Db:
        def get(self, model, _identifier):
            return {ArchiveObject: archive, ImportJob: job, SourceAccount: account}[model]

        def scalar(self, _statement):
            return job

        def execute(self, _statement):
            return _Rows([("versioned-fixture", "1")])

        def rollback(self):
            pass

    class Worker:
        def __init__(self, _db):
            pass

        def check_ownership(self, _job, _token):
            pass

        def attempt_staging_path(self, root_path, _job):
            return str(root_path / "staging" / "attempt")

        def mark_completed(self, *_args, **_kwargs):
            return True

    class Service:
        def __init__(self, _db):
            pass

        def import_fixture(self, _payload, _archive_id, **kwargs):
            assert kwargs["parser"] is v1
            tuple(kwargs["parser"].parse(_payload))
            return SimpleNamespace(report=empty_report(v1.name, v1.version, "matched"), counts={})

    monkeypatch.setattr(processor_module, "ImportWorker", Worker)
    monkeypatch.setattr(processor_module, "ImportService", Service)

    ImportProcessor(Db(), root, registry=ParserRegistry([V2(), v1])).process(job, uuid.uuid4())

    assert executions == ["v1", "v1"]


def test_no_ledger_leaves_parser_unpinned_for_failed_job_detection():
    parser, reason = resolve_durable_parser_contract(
        _job("retired", "failed"), [], registry=ParserRegistry([ParserV2()])
    )
    assert parser is None and reason is None


def test_worker_retry_atomically_revalidates_durable_parser_identity(pinning_db):
    db = pinning_db
    job = _durable_job(db, [("versioned-fixture", "1")])

    assert not ImportWorker(db).retry(job.id)
    assert not ImportWorker(db).retry(job.id, "versioned-fixture", "2")
    db.expire_all()
    assert db.get(ImportJob, job.id).status == "partial"
    assert ImportWorker(db).retry(job.id, "versioned-fixture", "1")
    db.expire_all()
    assert db.get(ImportJob, job.id).status == "pending"
    assert db.get(ImportJob, job.id).attempt_count == 1


def test_worker_retry_without_identity_cannot_bypass_ledger_free_partial(pinning_db):
    db = pinning_db
    job = _durable_job(db, [], status="partial")

    assert not ImportWorker(db).retry(job.id)
    db.expire_all()
    assert db.get(ImportJob, job.id).status == "partial"


def test_ledger_free_failed_retry_atomically_adopts_selected_parser_identity(pinning_db):
    db = pinning_db
    job = _durable_job(db, [], status="failed")
    job.parser_name = "retired-parser"
    job.parser_version = "retired-version"
    db.commit()

    assert ImportWorker(db).retry(job.id, "versioned-fixture", "2")
    db.expire_all()
    retried = db.get(ImportJob, job.id)
    assert retried.status == "pending"
    assert (retried.parser_name, retried.parser_version) == ("versioned-fixture", "2")


def test_v1_durable_replay_completes_with_v1_while_v2_is_default(
    pinning_db, monkeypatch, tmp_path: Path
):
    db = pinning_db
    root = tmp_path / "data"

    def conversation(identifier):
        return {
            "id": identifier,
            "title": identifier,
            "current_node": "node",
            "mapping": {
                "node": {
                    "parent": None,
                    "message": {
                        "author": {"role": "user"},
                        "content": {"parts": [identifier]},
                    },
                },
            },
        }

    first_payload = {
        "schema_marker": "qingyu-fixture-v1",
        "conversations": [conversation("first")],
    }
    retry_payload = {
        "schema_marker": "qingyu-fixture-v1",
        "future_top_level": "preserve-as-warning",
        "conversations": [conversation("first"), conversation("second")],
    }
    source = tmp_path / "retry.zip"
    with zipfile.ZipFile(source, "w") as bundle:
        bundle.writestr("fixture.json", json.dumps(retry_payload))
    content = source.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    relative = f"archive/objects/{digest[:2]}/{digest}"
    archived = root / relative
    archived.parent.mkdir(parents=True)
    archived.write_bytes(content)

    account = SourceAccount(platform="pinning-e2e", account_label=str(uuid.uuid4()), metadata_json={})
    archive = ArchiveObject(
        sha256=digest,
        size_bytes=len(content),
        relative_path=relative,
        media_type="application/zip",
        integrity_status="verified",
        metadata_json={},
    )
    db.add_all([account, archive]); db.flush()
    job = ImportJob(
        source_account_id=account.id,
        archive_object_id=archive.id,
        parser_name=ParserV1.name,
        parser_version=ParserV1.version,
        status="pending",
        attempt_count=0,
        report_json={},
    )
    db.add(job); db.commit()

    initial = ImportWorker(db).claim_next(60)
    ImportService(db).import_fixture(
        first_payload,
        archive.id,
        job=initial,
        lease_token=initial.lease_token,
        parser=ParserV1(),
    )
    assert ImportWorker(db).mark_failed_or_partial(
        initial.id, initial.lease_token, "second_batch_failed"
    ) == "partial"

    v1_calls = []

    class TrackingV1(ParserV1):
        def parse(self, payload):
            v1_calls.append("v1")
            return super().parse(payload)

    class RejectingV2(ParserV2):
        def can_parse(self, _payload):
            raise AssertionError("durable replay must not detect the default v2 parser")

        def parse(self, _payload):
            raise AssertionError("durable replay must not execute v2")

    v1 = TrackingV1()
    v2 = RejectingV2()
    monkeypatch.setattr(api_import, "ParserRegistry", lambda: ParserRegistry([v2, v1]))
    response = TestClient(app).post(f"/api/v1/import-jobs/{job.id}/retry", headers=HEADERS)
    assert response.status_code == 202

    db.expire_all()
    retry_claim = ImportWorker(db).claim_next(60)
    result = ImportProcessor(db, root, registry=ParserRegistry([v2, v1])).process(
        retry_claim, retry_claim.lease_token
    )

    db.expire_all()
    completed = db.get(ImportJob, job.id)
    identities = db.query(
        ImportBatchCommit.parser_name, ImportBatchCommit.contract_version
    ).filter(ImportBatchCommit.import_job_id == job.id).all()
    assert completed.status == "completed"
    assert completed.report_json["parser"]["name"] == ParserV1.name
    assert any(item.get("field") == "future_top_level" for item in completed.report_json["unknown"])
    assert result["parser"]["version"] == ParserV1.version
    assert identities == [(ParserV1.name, ParserV1.version)] * 2
    assert v1_calls == ["v1"]


def test_real_retry_api_unavailable_and_conflict_have_zero_state_change(pinning_db, monkeypatch, tmp_path):
    db = pinning_db
    unavailable = _durable_job(db, [("versioned-fixture", "1")])
    conflict = _durable_job(db, [("versioned-fixture", "1"), ("versioned-fixture", "2")])
    monkeypatch.setattr(api_import, "ParserRegistry", lambda: ParserRegistry([ParserV2()]))
    client = TestClient(app)

    first = client.post(f"/api/v1/import-jobs/{unavailable.id}/retry", headers=HEADERS)
    second = client.post(f"/api/v1/import-jobs/{conflict.id}/retry", headers=HEADERS)

    assert first.status_code == second.status_code == 409
    assert first.json()["detail"] == PARSER_CONTRACT_UNAVAILABLE
    assert second.json()["detail"] == PARSER_CONTRACT_CONFLICT
    db.expire_all()
    for job_id in (unavailable.id, conflict.id):
        unchanged = db.get(ImportJob, job_id)
        assert unchanged.status == "partial"
        assert unchanged.attempt_count == 1
        assert unchanged.checkpoint_sequence == 0
    assert not (tmp_path / "staging").exists()
