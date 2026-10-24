import hashlib
import io
import json
import os
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path

import pytest
from app.config import get_settings
from app.db import get_db
from app.export.bundle import verify_bundle
from app.export.service import ExportService
from app.importers.worker_runtime import WorkerRuntime
from app.main import app
from app.models import ArchiveObject, Conversation, SourceAccount
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

GATEWAY_DIR = Path(__file__).resolve().parents[1]
READ_HEADERS = {"Authorization": "Bearer qingyu-test-evidence-read-token-only"}
API_HEADERS = {"Authorization": "Bearer qingyu-test-token-only"}
EVIDENCE_TABLES = (
    "mapping_node_observations",
    "conversation_observations",
    "attachment_observations",
    "attachments",
    "message_observations",
    "message_revisions",
    "messages",
    "conversation_nodes",
    "import_batch_commits",
    "import_jobs",
    "conversations",
    "archive_observations",
    "source_accounts",
    "archive_objects",
)


def _database_name() -> str:
    return f"qingyu_export_{uuid.uuid4().hex[:16]}"


def _scrub(message: str, url: str) -> str:
    password = make_url(url).password or ""
    if password:
        message = message.replace(password, "***")
    return message


def _alembic(url: str) -> None:
    env = os.environ.copy()
    env.update(
        {
            "APP_ENV": "test",
            "DATABASE_URL": url,
            "API_TOKEN": "qingyu-test-token-only",
            "EVIDENCE_READ_API_TOKEN": "qingyu-test-evidence-read-token-only",
            "EVIDENCE_CURSOR_SIGNING_KEY": "qingyu-test-server-only-cursor-signing-key",
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=GATEWAY_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(_scrub((result.stderr or "") + (result.stdout or ""), url))


def _snapshot(engine) -> tuple:
    with engine.connect() as connection:
        revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        rows = []
        for table in EVIDENCE_TABLES:
            digest = connection.scalar(
                text(
                    f"SELECT md5(coalesce(string_agg({table}::text, ',' ORDER BY id::text), '')) "
                    f"FROM {table}"
                )
            )
            count = connection.scalar(text(f"SELECT count(*) FROM {table}"))
            rows.append((table, count, digest))
    return (revision, tuple(rows))


def _truncate(engine) -> None:
    names = ", ".join(EVIDENCE_TABLES)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text(f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE"))


def _chatgpt_zip(conversation_external_id: str) -> bytes:
    payload = [
        {
            "id": conversation_external_id,
            "title": "清语记忆",
            "create_time": 1.0,
            "update_time": 2.0,
            "current_node": "leaf",
            "mapping": {
                "root": {
                    "id": "root",
                    "parent": None,
                    "children": ["leaf"],
                    "message": None,
                },
                "leaf": {
                    "id": "leaf",
                    "parent": "root",
                    "children": [],
                    "message": {
                        "id": "message-1",
                        "author": {"role": "user"},
                        "content": {"content_type": "text", "parts": ["hello export"]},
                        "create_time": 1.5,
                    },
                },
            },
        }
    ]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("conversations.json", json.dumps(payload))
    return buffer.getvalue()


def _member(blob: bytes, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        return archive.read(name)


def _names(blob: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        return archive.namelist()


@pytest.fixture(scope="module")
def export_engine():
    base = make_url(os.environ["TEST_DATABASE_URL"])
    name = _database_name()
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    target_url = base.set(database=name).render_as_string(hide_password=False)
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        _alembic(target_url)
        engine = create_engine(target_url, pool_pre_ping=True)
        try:
            yield engine
        finally:
            engine.dispose()
    finally:
        with admin.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


@pytest.fixture
def export_app(export_engine, tmp_path, monkeypatch):
    _truncate(export_engine)
    monkeypatch.setattr(get_settings(), "archive_root", str(tmp_path))
    factory = sessionmaker(bind=export_engine, autoflush=False, expire_on_commit=False)

    def override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    try:
        yield TestClient(app), factory, tmp_path, export_engine
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_export_routes_are_get_only_and_split_credentials():
    export_paths = {
        path: sorted(operations)
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/api/v1/export")
    }
    assert export_paths == {
        "/api/v1/export/conversations/{conversation_id}": ["get"],
        "/api/v1/export/archive": ["get"],
    }
    client = TestClient(app)
    missing = client.get(f"/api/v1/export/conversations/{uuid.uuid4()}")
    assert missing.status_code == 401
    assert missing.json()["detail"] == {"code": "authentication_required"}
    archive = client.get("/api/v1/export/archive", headers=READ_HEADERS)
    assert archive.status_code == 401
    assert archive.json()["detail"] == "Invalid or missing bearer token"
    assert client.post(
        f"/api/v1/export/conversations/{uuid.uuid4()}", headers=API_HEADERS
    ).status_code == 405


def test_import_query_export_loop_is_read_only_and_verifiable(export_app):
    client, factory, root, engine = export_app
    external_id = f"export-{uuid.uuid4()}"
    upload = _chatgpt_zip(external_id)
    before_import_tables = _snapshot(engine)

    created = client.post(
        "/api/v1/import/chatgpt",
        headers=API_HEADERS,
        files={"file": ("export.zip", upload, "application/zip")},
    )
    assert created.status_code == 202, created.text
    job_id = created.json()["import_job_id"]
    assert WorkerRuntime(
        root,
        poll_interval=0.01,
        heartbeat_interval=0.05,
        lease_duration=5,
        session_factory=factory,
        cleanup_interval_seconds=10_000,
    ).run_once()
    job = client.get(f"/api/v1/import-jobs/{job_id}", headers=API_HEADERS)
    assert job.status_code == 200
    assert job.json()["status"] == "completed", job.json()

    with factory() as db:
        conversation_id = db.scalar(
            text("SELECT id FROM conversations WHERE external_conversation_id = :external"),
            {"external": external_id},
        )
    assert conversation_id is not None

    observations = client.get(
        "/api/v1/evidence/conversation-observations",
        headers=READ_HEADERS,
        params={"conversation_id": str(conversation_id), "limit": 100},
    )
    assert observations.status_code == 200
    assert observations.json()["consistency"] == "bounded_high_water_enumeration_v1"
    listed = observations.json()["items"]
    assert len(listed) == 1
    assert listed[0]["observed_title"] == "清语记忆"
    observation_id = listed[0]["conversation_observation_id"]
    nodes = client.get(
        f"/api/v1/evidence/conversation-observations/{observation_id}/nodes",
        headers=READ_HEADERS,
        params={"limit": 100},
    )
    assert nodes.status_code == 200
    node_items = nodes.json()["items"]
    texts = [
        item["message"]["content_text"]
        for item in node_items
        if item["message"] is not None
    ]
    assert texts == ["hello export"]

    (root / "staging").mkdir(exist_ok=True)
    marker = "staging-marker-" + uuid.uuid4().hex
    (root / "staging" / "not-evidence.txt").write_text(marker, encoding="utf-8")
    before_export = _snapshot(engine)

    first = client.get(
        f"/api/v1/export/conversations/{conversation_id}",
        headers=READ_HEADERS,
    )
    second = client.get(
        f"/api/v1/export/conversations/{conversation_id}",
        headers=API_HEADERS,
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    for response in (first, second):
        assert response.headers["content-type"].startswith("application/zip")
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-qingyu-export-format"] == "qingyu-export-bundle-v1"
        verified = verify_bundle(response.content)
        assert verified["manifest_sha256"] == response.headers["x-qingyu-manifest-sha256"]
        assert verified["manifest"]["scope"] == "conversation"
        assert verified["manifest"]["conversation_id"] == str(conversation_id)
        assert verified["manifest"]["high_water"]["conversation_observation_id"] == observation_id
    evidence = json.loads(_member(first.content, "evidence/conversation-observations.json"))
    assert evidence["items"] == listed
    exported_nodes = json.loads(
        _member(first.content, f"evidence/observations/{observation_id}/nodes.json")
    )
    assert exported_nodes["items"] == node_items
    assert exported_nodes["observation"]["conversation_observation_id"] == observation_id
    referenced = json.loads(_member(first.content, "archive/referenced-objects.json"))
    assert len(referenced) == 1
    assert referenced[0]["sha256"] == hashlib.sha256(upload).hexdigest()
    assert referenced[0]["size_bytes"] == len(upload)
    assert "archive/objects" not in first.content.decode("utf-8", errors="ignore")
    assert marker not in first.content.decode("utf-8", errors="ignore")
    assert "qingyu-test-token-only" not in first.content.decode("utf-8", errors="ignore")
    assert all(not name.startswith("objects/") for name in _names(first.content))
    first_files = verify_bundle(first.content)["manifest"]["files"]
    second_files = verify_bundle(second.content)["manifest"]["files"]
    assert first_files == second_files

    archive = client.get("/api/v1/export/archive", headers=API_HEADERS)
    assert archive.status_code == 200, archive.text
    verified_archive = verify_bundle(archive.content)
    assert verified_archive["manifest_sha256"] == archive.headers["x-qingyu-manifest-sha256"]
    assert verified_archive["manifest"]["scope"] == "archive"
    assert verified_archive["manifest"]["conversation_id"] is None
    digest = hashlib.sha256(upload).hexdigest()
    assert _member(archive.content, f"objects/{digest}") == upload
    metadata = json.loads(_member(archive.content, f"objects/{digest}.json"))
    assert metadata["sha256"] == digest
    assert metadata["object_id"] == referenced[0]["object_id"]
    assert "relative_path" not in metadata
    assert marker.encode() not in archive.content
    archived_observations = json.loads(
        _member(archive.content, "evidence/conversation-observations.json")
    )
    assert archived_observations["items"] == listed
    assert _snapshot(engine) == before_export
    assert before_export[0] == before_import_tables[0]

    with factory() as db:
        service = ExportService(db, root)
        bundle = None
        try:
            bundle = service.export_conversation(conversation_id)
            assert verify_bundle(bundle.path.read_bytes())["manifest"]["files"] == first_files
            mode = db.execute(text("SHOW transaction_read_only")).scalar()
            assert str(mode).strip() == "on"
            with pytest.raises(SQLAlchemyError):
                db.execute(text("UPDATE conversations SET title = title"))
        finally:
            if bundle is not None:
                bundle.unlink()
            db.rollback()


def test_missing_and_empty_conversations_and_empty_archive(export_app):
    client, factory, _root, engine = export_app
    missing = client.get(
        f"/api/v1/export/conversations/{uuid.uuid4()}",
        headers=READ_HEADERS,
    )
    assert missing.status_code == 404
    assert missing.json()["detail"] == {"code": "conversation_not_found"}

    with factory() as db:
        account = SourceAccount(
            platform="chatgpt",
            account_label=f"empty-{uuid.uuid4()}",
            metadata_json={},
        )
        db.add(account)
        db.flush()
        conversation = Conversation(
            source_account_id=account.id,
            external_conversation_id=f"empty-{uuid.uuid4()}",
            title="empty",
            metadata_json={},
        )
        db.add(conversation)
        db.commit()
        conversation_id = conversation.id
    before = _snapshot(engine)
    exported = client.get(
        f"/api/v1/export/conversations/{conversation_id}",
        headers=READ_HEADERS,
    )
    assert exported.status_code == 200, exported.text
    verified = verify_bundle(exported.content)
    evidence = json.loads(_member(exported.content, "evidence/conversation-observations.json"))
    assert evidence["items"] == []
    assert verified["manifest"]["high_water"] is None
    assert json.loads(_member(exported.content, "archive/referenced-objects.json")) == []
    assert _snapshot(engine) == before

    _truncate(engine)
    empty_archive = client.get("/api/v1/export/archive", headers=API_HEADERS)
    assert empty_archive.status_code == 200, empty_archive.text
    verified_archive = verify_bundle(empty_archive.content)
    assert verified_archive["manifest"]["files"] == [
        {
            "path": "evidence/conversation-observations.json",
            "sha256": hashlib.sha256(
                _member(empty_archive.content, "evidence/conversation-observations.json")
            ).hexdigest(),
            "size_bytes": len(_member(empty_archive.content, "evidence/conversation-observations.json")),
        }
    ]
    assert json.loads(_member(empty_archive.content, "evidence/conversation-observations.json"))["items"] == []


def test_archive_export_fails_closed_on_corrupt_or_linked_bytes(export_app):
    client, factory, root, engine = export_app
    payload = b"raw-archive-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    relative = Path("archive") / "objects" / digest[:2] / digest
    target = root / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)
    with factory() as db:
        record = ArchiveObject(
            sha256=digest,
            size_bytes=len(payload),
            relative_path=relative.as_posix(),
            media_type="application/octet-stream",
            integrity_status="verified",
            metadata_json={},
        )
        db.add(record)
        db.commit()
        object_id = record.id

    before = _snapshot(engine)
    exported = client.get("/api/v1/export/archive", headers=API_HEADERS)
    assert exported.status_code == 200, exported.text
    assert _member(exported.content, f"objects/{digest}") == payload
    assert _snapshot(engine) == before

    target.write_bytes(b"corrupt-bytes")
    failed = client.get("/api/v1/export/archive", headers=API_HEADERS)
    assert failed.status_code == 409
    assert failed.json()["detail"] == {"code": "export_integrity_failure"}
    assert _snapshot(engine) == before

    target.write_bytes(payload)
    outside = root / "outside.bin"
    outside.write_bytes(payload)
    target.unlink()
    target.symlink_to(outside)
    linked = client.get("/api/v1/export/archive", headers=API_HEADERS)
    assert linked.status_code == 409
    assert linked.json()["detail"] == {"code": "export_integrity_failure"}
    assert _snapshot(engine) == before

    with factory() as db:
        stored = db.get(ArchiveObject, object_id)
        assert stored is not None
        assert stored.sha256 == digest
        assert stored.integrity_status == "verified"
        assert stored.integrity_checked_at is None
