import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.importers.generic import GenericTreeParser
from app.importers.report import empty_report
from app.importers.service import ImportService
from app.importers.worker import (
    DurableLedgerInconsistent,
    ImportWorker,
    aggregate_durable_job_state,
)
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
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

DB_URL = os.environ["TEST_DATABASE_URL"]
HEADERS = {"Authorization": "Bearer qingyu-test-token-only"}


@pytest.fixture()
def durable_env():
    engine = create_engine(DB_URL)
    with Session(engine, expire_on_commit=False) as db:
        models = (
            AttachmentObservation, Attachment, MessageObservation, MessageRevision,
            Message, ImportBatchCommit, Conversation, ArchiveObservation, ImportJob, SourceAccount, ArchiveObject,
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


def _claimed(db, *, max_attempts=3, attempt_count=0):
    account = SourceAccount(platform="durable-report", account_label=str(uuid.uuid4()), metadata_json={})
    archive = ArchiveObject(sha256=uuid.uuid4().hex * 2, size_bytes=1, relative_path="unused", media_type="x", integrity_status="verified", metadata_json={})
    db.add_all([account, archive]); db.flush()
    job = ImportJob(source_account_id=account.id, archive_object_id=archive.id, parser_name="fixture", parser_version="0.1", status="pending", attempt_count=attempt_count, max_attempts=max_attempts, report_json={})
    db.add(job); db.commit()
    return ImportWorker(db).claim_next(60)


def _batch(db, claim, sequence, *, new_messages=1, anomaly=None, warning=None, revisions=0):
    conversation = Conversation(source_account_id=claim.source_account_id, external_conversation_id=f"c-{sequence}", metadata_json={})
    db.add(conversation); db.flush()
    delta = empty_report("fixture", "0.1", "matched")
    delta["conversations"]["new"] = 1
    delta["messages"]["new"] = new_messages
    delta["revisions"]["new"] = revisions
    if anomaly:
        delta["tree_anomalies"].append({"type": anomaly})
    if warning:
        delta["unknown"].append({"type": warning, "severity": "warning"})
    db.add(ImportBatchCommit(
        import_job_id=claim.id, conversation_id=conversation.id, sequence_no=sequence,
        batch_key=f"batch-{sequence}", external_conversation_id=conversation.external_conversation_id,
        parser_name="fixture", contract_version="0.1", report_delta=delta,
        counts_delta={"discovered_count": new_messages, "imported_count": new_messages, "skipped_count": 0, "failed_count": 0},
    ))
    db.commit()


def test_exception_partial_and_api_get_use_detailed_durable_ledger(durable_env):
    db = durable_env; claim = _claimed(db)
    _batch(db, claim, 1, anomaly="durable-a", warning="durable-warning", revisions=1)
    _batch(db, claim, 2, new_messages=2, anomaly="durable-b", revisions=2)

    terminal = {"messages": {"new": 999}, "tree_anomalies": [{"type": "rolled-back"}], "errors": [{"type": "processor_error"}]}
    assert ImportWorker(db).mark_failed_or_partial(claim.id, claim.lease_token, "processor_error", terminal) == "partial"

    response = TestClient(app).get(f"/api/v1/import-jobs/{claim.id}", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["counts"] == {"discovered": 3, "imported": 3, "skipped": 0, "failed": 0}
    assert body["report"]["messages"]["new"] == 3
    assert body["report"]["conversations"]["new"] == 2
    assert body["report"]["revisions"]["new"] == 3
    assert body["report"]["tree_anomalies"] == [{"type": "durable-a"}, {"type": "durable-b"}]
    assert body["report"]["unknown"] == [{"type": "durable-warning", "severity": "warning"}]
    assert body["report"]["errors"] == [{"type": "processor_error"}]


def test_exhausted_stale_recovery_uses_same_durable_snapshot(durable_env):
    db = durable_env; claim = _claimed(db, max_attempts=1)
    _batch(db, claim, 1, new_messages=2, anomaly="committed")
    job = db.get(ImportJob, claim.id)
    job.report_json = {"messages": {"new": 700}, "errors": [{"type": "crash"}]}
    job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()

    assert ImportWorker(db).recover_stale() == 1
    db.expire_all(); recovered = db.get(ImportJob, claim.id)
    assert recovered.status == "partial"
    assert recovered.imported_count == 2
    assert recovered.report_json["messages"]["new"] == 2
    assert recovered.report_json["tree_anomalies"] == [{"type": "committed"}]
    assert recovered.report_json["errors"] == [{"type": "crash"}]


def test_normal_completion_rebuilds_same_snapshot_and_current_attempt_attachments(durable_env):
    db = durable_env; claim = _claimed(db, attempt_count=1)
    _batch(db, claim, 1)
    conversation = db.query(Conversation).one()
    message = Message(conversation_id=conversation.id, external_message_id="m")
    db.add(message); db.flush()
    revision = MessageRevision(message_id=message.id, revision_no=1, role="user", content_text="x", content_json={}, content_hash="x", revision_fingerprint="f")
    db.add(revision); db.flush()
    attachment = Attachment(message_revision_id=revision.id, stable_attachment_key="a", descriptor_json={})
    db.add(attachment); db.flush()
    db.add_all([
        AttachmentObservation(import_job_id=claim.id, attempt_number=1, attachment_id=attachment.id, availability="failed", metadata_json={}),
        AttachmentObservation(import_job_id=claim.id, attempt_number=2, attachment_id=attachment.id, availability="available", archive_object_id=claim.archive_object_id, metadata_json={}),
    ])
    db.commit()

    supplied = empty_report("fixture", "0.1", "matched")
    supplied["messages"]["new"] = 999
    supplied["attachments"]["failed"] = 999
    assert ImportWorker(db).mark_completed(claim.id, claim.lease_token, supplied, counts={"imported_count": 999})
    db.expire_all(); completed = db.get(ImportJob, claim.id)
    assert completed.imported_count == 1
    assert completed.report_json["messages"]["new"] == 1
    assert completed.report_json["attachments"] == {"available": 1, "missing": 0, "referenced_only": 0, "failed": 0}


def test_terminal_unknown_diagnostic_is_safely_overlaid_on_durable_truth(durable_env):
    db = durable_env; claim = _claimed(db)
    _batch(db, claim, 1)
    terminal = {
        "messages": {"new": 999},
        "unknown": [{
            "type": "unsupported_structure",
            "detail": "no registered parser matched",
            "severity": "error",
            "untrusted_extra": {"messages": {"new": 999}},
        }],
    }

    ImportWorker(db).mark_failed_or_partial(claim.id, claim.lease_token, "unsupported_structure", terminal)
    db.expire_all(); job = db.get(ImportJob, claim.id)

    assert job.report_json["messages"]["new"] == 1
    assert job.report_json["unknown"] == [{
        "type": "unsupported_structure",
        "detail": "no registered parser matched",
        "severity": "error",
    }]


def test_inconsistent_durable_count_and_report_delta_fails_closed(durable_env):
    db = durable_env; claim = _claimed(db)
    _batch(db, claim, 1)
    batch = db.query(ImportBatchCommit).one()
    batch.counts_delta = {**batch.counts_delta, "imported_count": 99}
    db.commit()

    with pytest.raises(DurableLedgerInconsistent, match="inconsistent_durable_batch_counts"):
        aggregate_durable_job_state(db, claim.id, claim.attempt_count)


def test_real_service_persists_and_rebuilds_the_selected_parser_contract(durable_env):
    db = durable_env; claim = _claimed(db)

    class HistoricalParser(GenericTreeParser):
        name = "fixture-history"
        version = "7"

    payload = {
        "schema_marker": "qingyu-fixture-v1",
        "conversations": [{
            "id": "parser-contract",
            "title": "parser-contract",
            "current_node": "node",
            "mapping": {
                "node": {
                    "parent": None,
                    "message": {
                        "author": {"role": "user"},
                        "content": {"parts": ["content"]},
                    },
                },
            },
        }],
    }
    outcome = ImportService(db).import_fixture(
        payload,
        claim.archive_object_id,
        job=claim,
        lease_token=claim.lease_token,
        parser=HistoricalParser(),
        unknown_entries=[{"type": "unknown_field", "severity": "warning", "field": "future"}],
    )
    snapshot = aggregate_durable_job_state(db, claim.id, claim.attempt_count)

    assert outcome.report["parser"] == {
        "name": "fixture-history", "version": "7", "detection": "matched"
    }
    assert snapshot.report["parser"] == outcome.report["parser"]
    assert snapshot.report["unknown"] == [
        {"type": "unknown_field", "severity": "warning", "field": "future"}
    ]

    replay = ImportService(db).import_fixture(
        payload,
        claim.archive_object_id,
        job=claim,
        lease_token=claim.lease_token,
        parser=HistoricalParser(),
        unknown_entries=[{
            "type": "unknown_field", "severity": "warning", "field": "later_contract_field"
        }],
    )
    assert any(item.get("field") == "later_contract_field" for item in replay.report["unknown"])
    assert ImportWorker(db).mark_completed(
        claim.id, claim.lease_token, replay.report, counts=replay.counts
    )
    db.expire_all()
    assert any(
        item.get("field") == "later_contract_field"
        for item in db.get(ImportJob, claim.id).report_json["unknown"]
    )


def test_archive_integrity_terminalization_keeps_durable_report(durable_env, tmp_path):
    db = durable_env; claim = _claimed(db)
    _batch(db, claim, 1, new_messages=2, anomaly="committed")

    ImportWorker(db).mark_archive_integrity_failure(
        job_id=claim.id,
        token=claim.lease_token,
        archive_object_id=claim.archive_object_id,
        archive_root=tmp_path,
        integrity_error="archive_integrity_failure: digest_or_size_mismatch",
        report={"errors": [{"type": "archive_integrity_failure", "severity": "error"}]},
    )

    db.expire_all(); job = db.get(ImportJob, claim.id)
    assert job.status == "partial"
    assert job.imported_count == 2
    assert job.report_json["messages"]["new"] == 2
    assert job.report_json["tree_anomalies"] == [{"type": "committed"}]
    assert job.report_json["errors"] == [{"type": "archive_integrity_failure", "severity": "error"}]


def test_durable_report_uses_sequence_order_not_insert_order(durable_env):
    db = durable_env; claim = _claimed(db)
    _batch(db, claim, 2, anomaly="second")
    _batch(db, claim, 1, anomaly="first")

    snapshot = aggregate_durable_job_state(db, claim.id, claim.attempt_count)

    assert snapshot.report["tree_anomalies"] == [{"type": "first"}, {"type": "second"}]


def test_inconsistent_durable_parser_metadata_fails_closed(durable_env):
    db = durable_env; claim = _claimed(db)
    _batch(db, claim, 1)
    batch = db.query(ImportBatchCommit).one()
    batch.report_delta = {
        **batch.report_delta,
        "parser": {"name": "wrong", "version": "0.1", "detection": "matched"},
    }
    db.commit()

    with pytest.raises(DurableLedgerInconsistent, match="inconsistent_durable_parser_identity"):
        aggregate_durable_job_state(db, claim.id, claim.attempt_count)


def test_malformed_ledger_terminalization_rolls_back_atomically(durable_env):
    db = durable_env; claim = _claimed(db)
    _batch(db, claim, 1)
    batch = db.query(ImportBatchCommit).one()
    batch.counts_delta = {**batch.counts_delta, "imported_count": 99}
    db.commit()

    with pytest.raises(DurableLedgerInconsistent, match="inconsistent_durable_batch_counts"):
        ImportWorker(db).mark_completed(claim.id, claim.lease_token, empty_report())

    db.expire_all(); unchanged = db.get(ImportJob, claim.id)
    assert unchanged.status == "running"
    assert unchanged.lease_token == claim.lease_token
    assert unchanged.imported_count == 0
    assert unchanged.report_json == {}


def test_zero_batch_completion_rejects_count_report_disagreement(durable_env):
    db = durable_env; claim = _claimed(db)
    report = empty_report("fixture", "0.1", "matched")
    report["messages"]["new"] = 1

    with pytest.raises(DurableLedgerInconsistent, match="inconsistent_terminal_counts"):
        ImportWorker(db).mark_completed(
            claim.id,
            claim.lease_token,
            report,
            counts={"imported_count": 0},
        )

    db.expire_all(); unchanged = db.get(ImportJob, claim.id)
    assert unchanged.status == "running"
    assert unchanged.lease_token == claim.lease_token
