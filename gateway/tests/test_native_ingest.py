"""qingyu_native ingest: ledger idempotency, not a second queue."""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from app.archive.manifest import manifest_is_finalized
from app.config import Settings, get_settings, parse_ingest_clients
from app.db import engine
from app.importers.revision import compute_revision_fingerprint
from app.importers.worker_runtime import WorkerRuntime
from app.ingest.canonical import message_record, native_document, payload_hash
from app.main import app
from app.models import (
    ArchiveObject,
    ArchiveObservation,
    Conversation,
    ConversationNode,
    ConversationObservation,
    ImportBatchCommit,
    ImportJob,
    IngestRequest,
    MappingNodeObservation,
    Message,
    MessageObservation,
    MessageRevision,
    SourceAccount,
)
from fastapi.testclient import TestClient
from pydantic_settings import SettingsConfigDict
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

CLIENT_ID = "native-ingest-test"
OTHER_CLIENT = "native-ingest-other"
RAW_TOKEN = "native-ingest-raw-token"
OTHER_TOKEN = "native-ingest-other-token"
ROTATED_TOKEN = "native-ingest-rotated-token"
API_TOKEN = "qingyu-test-token-only"
READ_TOKEN = "qingyu-test-evidence-read-token-only"
PHRASE = "native-ingest-phrase"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _install_clients(monkeypatch) -> None:
    directory = (
        f"{CLIENT_ID}:{_hash(RAW_TOKEN)},{CLIENT_ID}:{_hash(ROTATED_TOKEN)},"
        f"{OTHER_CLIENT}:{_hash(OTHER_TOKEN)}"
    )
    monkeypatch.setattr(get_settings(), "ingest_clients", directory)
    monkeypatch.setattr(get_settings(), "ingest_sync_wait_seconds", 0.0)


def _headers(token: str = RAW_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _message_body(
    request_id: str,
    text: str = PHRASE,
    *,
    message_id: str = "msg-1",
    parent: str | None = None,
    content_kind: str = "conversation_message",
    role: str = "user",
    conversation_id: str = "conv-native-1",
) -> dict:
    message = {
        "client_message_id": message_id,
        "role": role,
        "content_text": text,
        "content_kind": content_kind,
    }
    if parent is not None:
        message["parent_client_message_id"] = parent
    return {
        "client_request_id": request_id,
        "source_platform": "cursor",
        "source_conversation_id": conversation_id,
        "title": "Native notes",
        "message": message,
    }


def _expected_hash(body: dict, client_id: str = CLIENT_ID) -> str:
    message = body["message"]
    document = native_document(
        kind="messages",
        source_platform=body["source_platform"],
        client_id=client_id,
        source_conversation_id=body["source_conversation_id"],
        title=body["title"],
        messages=[message_record(
            client_message_id=message["client_message_id"],
            parent_client_message_id=message.get("parent_client_message_id"),
            role=message["role"],
            content_text=message["content_text"],
            content_kind=message["content_kind"],
            model=message.get("model"),
            timestamp=message.get("timestamp"),
        )],
    )
    return payload_hash(document)


def _purge(db: Session) -> None:
    client_ids = (CLIENT_ID, OTHER_CLIENT)
    db.execute(delete(IngestRequest).where(IngestRequest.client_id.in_(client_ids)))
    accounts = list(db.scalars(select(SourceAccount).where(SourceAccount.account_label.in_(client_ids))))
    for account in accounts:
        jobs = list(db.scalars(select(ImportJob).where(ImportJob.source_account_id == account.id)))
        job_ids = [job.id for job in jobs]
        if job_ids:
            db.execute(delete(MessageObservation).where(MessageObservation.import_job_id.in_(job_ids)))
            batch_ids = list(db.scalars(
                select(ImportBatchCommit.id).where(ImportBatchCommit.import_job_id.in_(job_ids))
            ))
            if batch_ids:
                observation_ids = list(db.scalars(
                    select(ConversationObservation.id).where(
                        ConversationObservation.import_batch_commit_id.in_(batch_ids)
                    )
                ))
                if observation_ids:
                    db.execute(delete(MappingNodeObservation).where(
                        MappingNodeObservation.conversation_observation_id.in_(observation_ids)
                    ))
                    db.execute(delete(ConversationObservation).where(
                        ConversationObservation.id.in_(observation_ids)
                    ))
            db.execute(delete(ImportBatchCommit).where(ImportBatchCommit.import_job_id.in_(job_ids)))
        conversation_ids = list(db.scalars(
            select(Conversation.id).where(Conversation.source_account_id == account.id)
        ))
        if conversation_ids:
            message_ids = list(db.scalars(
                select(Message.id).where(Message.conversation_id.in_(conversation_ids))
            ))
            if message_ids:
                db.execute(delete(MessageRevision).where(MessageRevision.message_id.in_(message_ids)))
                db.execute(delete(Message).where(Message.id.in_(message_ids)))
            db.execute(delete(ConversationNode).where(ConversationNode.conversation_id.in_(conversation_ids)))
            db.execute(delete(Conversation).where(Conversation.id.in_(conversation_ids)))
        archive_ids = [job.archive_object_id for job in jobs]
        if job_ids:
            db.execute(delete(ArchiveObservation).where(ArchiveObservation.import_job_id.in_(job_ids)))
            db.execute(delete(ImportJob).where(ImportJob.id.in_(job_ids)))
        if archive_ids:
            db.execute(delete(ArchiveObservation).where(ArchiveObservation.archive_object_id.in_(archive_ids)))
            still = set(db.scalars(
                select(ImportJob.archive_object_id).where(ImportJob.archive_object_id.in_(archive_ids))
            ))
            removable = [item for item in archive_ids if item not in still]
            if removable:
                db.execute(delete(ArchiveObject).where(ArchiveObject.id.in_(removable)))
        db.delete(account)
    db.commit()


@pytest.fixture
def native_client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(get_settings(), "archive_root", str(tmp_path))
    _install_clients(monkeypatch)
    with Session(engine) as db:
        _purge(db)
    client = TestClient(app)
    try:
        yield client, tmp_path
    finally:
        with Session(engine) as db:
            _purge(db)


def _run_worker(root: Path, job_id) -> str:
    runtime = WorkerRuntime(root, poll_interval=0.01, heartbeat_interval=0.05, lease_duration=30)
    for _ in range(8):
        with Session(engine) as db:
            job = db.get(ImportJob, job_id)
            if job is not None and job.status in {"completed", "failed", "partial"}:
                return job.status
        runtime.run_once()
    with Session(engine) as db:
        job = db.get(ImportJob, job_id)
        return job.status if job is not None else "missing"


def test_content_kind_changes_revision_fingerprint():
    shared = dict(
        role="assistant",
        content_text="same words",
        content_json={"content_type": "text", "parts": ["same words"]},
        model=None,
        raw_timestamp=None,
        attachments=(),
    )
    spoken = compute_revision_fingerprint(
        **shared,
        semantic_evidence={"content_kind": "conversation_message", "provenance": "client_declared"},
    )
    summary = compute_revision_fingerprint(
        **shared,
        semantic_evidence={"content_kind": "client_generated_summary", "provenance": "client_declared"},
    )
    legacy = compute_revision_fingerprint(**shared)
    assert spoken != summary
    assert spoken != legacy


def test_ingest_clients_store_hashes_only():
    digest = _hash(RAW_TOKEN)
    parsed = parse_ingest_clients(f"  {CLIENT_ID}:{digest.upper()} , {CLIENT_ID}:{_hash(ROTATED_TOKEN)} ")
    assert parsed == {CLIENT_ID: (digest, _hash(ROTATED_TOKEN))}
    assert RAW_TOKEN not in str(parsed)
    with pytest.raises(ValueError):
        parse_ingest_clients(f"{CLIENT_ID}:{RAW_TOKEN}")


def test_missing_request_id_is_rejected_and_hash_is_not_accepted_as_one(native_client):
    client, _root = native_client
    body = _message_body("req-unused")
    body.pop("client_request_id")
    missing = client.post("/api/v1/ingest/messages", headers=_headers(), json=body)
    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "ingest_request_invalid"
    assert PHRASE not in missing.text

    digest = _expected_hash(_message_body("placeholder"))
    hashed = client.post(
        "/api/v1/ingest/messages",
        headers=_headers(),
        json=_message_body(digest),
    )
    assert hashed.status_code == 422
    assert hashed.json()["detail"]["code"] == "request_id_must_not_be_payload_hash"
    with Session(engine) as db:
        assert db.scalar(select(IngestRequest).where(IngestRequest.client_id == CLIENT_ID)) is None


def test_same_request_id_retries_and_a_different_body_conflicts(native_client):
    client, root = native_client
    first = client.post("/api/v1/ingest/messages", headers=_headers(), json=_message_body("req-retry"))
    assert first.status_code == 202
    assert first.json()["status"] == "processing"
    job_id = first.json()["import_job_id"]
    assert _run_worker(root, job_id) == "completed"

    again = client.post("/api/v1/ingest/messages", headers=_headers(), json=_message_body("req-retry"))
    assert again.status_code == 200
    body = again.json()
    assert body["status"] == "completed"
    assert body["import_job_id"] == job_id
    assert body["result"]["messages"][0]["client_message_id"] == "msg-1"
    assert PHRASE not in again.text

    conflict = client.post(
        "/api/v1/ingest/messages",
        headers=_headers(),
        json=_message_body("req-retry", "different body"),
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_key_reused"
    assert "different body" not in conflict.text
    assert "payload_hash" not in conflict.text
    with Session(engine) as db:
        rows = list(db.scalars(select(IngestRequest).where(IngestRequest.client_id == CLIENT_ID)))
        assert len(rows) == 1
        assert rows[0].payload_hash == _expected_hash(_message_body("req-retry"))
        assert rows[0].client_request_id == "req-retry"
        assert rows[0].client_request_id != rows[0].payload_hash
        conversation = db.scalar(select(Conversation).where(Conversation.external_conversation_id == "conv-native-1"))
        messages = list(db.scalars(select(Message).where(Message.conversation_id == conversation.id)))
        assert len(messages) == 1
        assert RAW_TOKEN not in (rows[0].client_id + rows[0].payload_hash + str(rows[0].result_json))


def test_same_payload_and_a_new_request_id_is_a_second_write(native_client):
    client, root = native_client
    left = client.post("/api/v1/ingest/messages", headers=_headers(), json=_message_body("req-left"))
    right = client.post("/api/v1/ingest/messages", headers=_headers(), json=_message_body("req-right"))
    assert left.status_code == 202 and right.status_code == 202
    assert left.json()["import_job_id"] != right.json()["import_job_id"]
    assert _run_worker(root, left.json()["import_job_id"]) == "completed"
    assert _run_worker(root, right.json()["import_job_id"]) == "completed"
    with Session(engine) as db:
        rows = list(db.scalars(
            select(IngestRequest).where(IngestRequest.client_id == CLIENT_ID).order_by(IngestRequest.client_request_id)
        ))
        assert [row.client_request_id for row in rows] == ["req-left", "req-right"]
        assert rows[0].payload_hash == rows[1].payload_hash
        assert all(row.client_request_id != row.payload_hash for row in rows)
        conversation = db.scalar(select(Conversation).where(Conversation.external_conversation_id == "conv-native-1"))
        observations = list(db.scalars(
            select(ConversationObservation).where(ConversationObservation.conversation_id == conversation.id)
        ))
        assert len(observations) == 2


def test_happy_path_publishes_evidence_on_the_existing_read_apis(native_client):
    client, root = native_client
    created = client.post("/api/v1/ingest/messages", headers=_headers(), json=_message_body("req-happy"))
    assert created.status_code == 202
    job_id = created.json()["import_job_id"]
    with Session(engine) as db:
        job = db.get(ImportJob, job_id)
        assert job.status == "pending"
        assert job.parser_name == "qingyu_native"
        archive = db.get(ArchiveObject, job.archive_object_id)
        ledger = db.scalar(select(IngestRequest).where(IngestRequest.import_job_id == job.id))
        assert ledger.payload_hash == archive.sha256
        assert manifest_is_finalized(root, archive.sha256)
        archived = (root / archive.relative_path).read_text(encoding="utf-8")
        assert "req-happy" not in archived
        assert "qingyu-native-v1" in archived
    assert _run_worker(root, job_id) == "completed"

    status = client.get("/api/v1/ingest/requests/req-happy", headers=_headers())
    assert status.status_code == 200
    finished = status.json()
    assert finished["status"] == "completed"
    observation_id = finished["result"]["conversation_observation_ids"][0]
    read = {"Authorization": f"Bearer {READ_TOKEN}"}
    page = client.get("/api/v1/evidence/conversation-observations?limit=50", headers=read)
    assert page.status_code == 200
    assert page.json()["contract_version"] == "evidence-query-v1"
    match = [item for item in page.json()["items"] if item["conversation_observation_id"] == observation_id]
    assert match and match[0]["parser_name"] == "qingyu_native"
    nodes = client.get(
        f"/api/v1/evidence/conversation-observations/{observation_id}/nodes",
        headers=read,
    )
    assert nodes.status_code == 200
    assert nodes.json()["contract_version"] == "evidence-query-v1"
    assert nodes.json()["items"][0]["message"]["content_text"] == PHRASE
    found = client.post(
        "/api/v1/evidence/search",
        headers=read,
        json={"q": PHRASE, "limit": 5},
    )
    assert found.status_code == 200
    assert found.json()["contract_version"] == "evidence-search-v1"
    assert any(item["conversation_observation_id"] == observation_id for item in found.json()["items"])
    with Session(engine) as db:
        observation = db.get(ConversationObservation, observation_id)
        assert observation.source_conversation_json["observation_scope"] == "append_delta"
        assert observation.source_conversation_json["provenance"] == "client_declared"
        account = db.scalar(select(SourceAccount).where(SourceAccount.account_label == CLIENT_ID))
        assert account.metadata_json["provenance"] == "client_declared"
        revision = db.scalar(
            select(MessageRevision)
            .join(Message, Message.id == MessageRevision.message_id)
            .where(MessageRevision.content_text == PHRASE)
        )
        assert revision.semantic_evidence_json["content_kind"] == "conversation_message"
        assert revision.semantic_revision_fingerprint is None
        assert revision.revision_fingerprint == compute_revision_fingerprint(
            role="user",
            content_text=PHRASE,
            content_json={"content_type": "text", "parts": [PHRASE]},
            model=None,
            raw_timestamp=None,
            attachments=(),
            semantic_evidence=revision.semantic_evidence_json,
        )


def test_append_delta_keeps_external_parent_and_downgrades_missing_parent(native_client):
    client, root = native_client
    first = client.post("/api/v1/ingest/messages", headers=_headers(), json=_message_body("req-parent", message_id="msg-a"))
    second_body = _message_body("req-child", message_id="msg-b", parent="msg-a", text="child line")
    second = client.post("/api/v1/ingest/messages", headers=_headers(), json=second_body)
    assert first.status_code == 202 and second.status_code == 202
    assert _run_worker(root, first.json()["import_job_id"]) == "completed"
    assert _run_worker(root, second.json()["import_job_id"]) == "completed"
    with Session(engine) as db:
        child_job = db.get(ImportJob, second.json()["import_job_id"])
        assert child_job.status == "completed"
        anomalies = child_job.report_json.get("tree_anomalies") or []
        unknown = child_job.report_json.get("unknown") or []
        assert not any(item.get("type") == "missing_parent" for item in anomalies)
        assert any(item.get("type") == "missing_parent" and item.get("severity") == "info" for item in unknown)
        child_observation = db.scalar(
            select(MessageObservation).where(MessageObservation.import_job_id == child_job.id)
        )
        assert child_observation.external_parent_node_id == "msg-a"
        assert child_observation.parent_message_id is None
        parent_node = db.scalar(select(ConversationNode).where(ConversationNode.external_node_id == "msg-a"))
        mapping = db.scalar(
            select(MappingNodeObservation)
            .join(ConversationNode, ConversationNode.id == MappingNodeObservation.conversation_node_id)
            .where(ConversationNode.external_node_id == "msg-b")
        )
        assert mapping.parent_conversation_node_id == parent_node.id


def test_ingest_token_does_not_open_operator_or_read_routes(native_client):
    client, _root = native_client
    denied = client.post("/api/v1/ingest/messages", json=_message_body("req-auth"))
    assert denied.status_code == 401
    assert client.post(
        "/api/v1/ingest/messages",
        headers=_headers(API_TOKEN),
        json=_message_body("req-auth"),
    ).status_code == 401
    assert client.post(
        "/api/v1/ingest/messages",
        headers=_headers(READ_TOKEN),
        json=_message_body("req-auth"),
    ).status_code == 401
    accepted = client.post(
        "/api/v1/ingest/messages",
        headers=_headers(ROTATED_TOKEN),
        json=_message_body("req-rotated"),
    )
    assert accepted.status_code == 202
    with Session(engine) as db:
        row = db.scalar(select(IngestRequest).where(IngestRequest.client_request_id == "req-rotated"))
        assert row.client_id == CLIENT_ID
    blocked_import = client.post(
        "/api/v1/import/chatgpt",
        headers=_headers(),
        files={"file": ("export.zip", b"not-a-zip", "application/zip")},
    )
    assert blocked_import.status_code == 401
    blocked_search = client.post(
        "/api/v1/evidence/search",
        headers=_headers(),
        json={"q": PHRASE},
    )
    assert blocked_search.status_code == 401


def test_ingest_requests_migration_downgrades_without_dropping_evidence(verified_test_database):
    gateway_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def alembic(*arguments: str) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            cwd=gateway_dir,
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr or completed.stdout

    try:
        alembic("downgrade", "0017_evidence_search_bigram")
        with verified_test_database.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0017_evidence_search_bigram"
            )
            assert connection.scalar(text("SELECT to_regclass('public.ingest_requests')")) is None
            assert connection.scalar(text("SELECT to_regclass('public.message_revisions')")) is not None
    finally:
        alembic("upgrade", "head")
    with verified_test_database.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0018_ingest_requests"
        assert connection.scalar(text("SELECT to_regclass('public.ingest_requests')")) is not None


class _IsolatedSettings(Settings):
    model_config = SettingsConfigDict(extra="ignore")

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        return (init_settings,)


def test_startup_report_does_not_print_ingest_hashes():
    digest = _hash(RAW_TOKEN)
    settings = _IsolatedSettings(
        app_env="test",
        api_token="a-real-token",
        evidence_read_api_token="another-real-token",
        evidence_cursor_signing_key="a-third-secret",
        ingest_clients=f"{CLIENT_ID}:{digest}",
        _env_file=None,
    )
    from app.config import startup_config_report

    report = startup_config_report(settings)
    assert "ingest_clients=yes" in report
    assert digest not in report
    assert RAW_TOKEN not in report
    assert CLIENT_ID not in report
