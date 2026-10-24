import io
import json
import zipfile
from pathlib import Path

from app.config import get_settings
from app.db import engine
from app.importers.generic import (
    canonical_attachment_descriptor,
    stable_attachment_keys,
)
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

HEADERS = {"Authorization": "Bearer qingyu-test-token-only"}


def _clear(db):
    for model in (
        Attachment,
        MessageObservation,
        MessageRevision,
        Message,
        Conversation,
        ArchiveObservation,
        ImportJob,
        SourceAccount,
        ArchiveObject,
    ):
        db.execute(delete(model))
    db.commit()


def _zip(member: str, payload: str | dict) -> bytes:
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr(member, payload if isinstance(payload, str) else json.dumps(payload))
    return content.getvalue()


def _mark_encrypted(content: bytes) -> bytes:
    """Set the ZIP encryption flag without requiring an external ZIP writer."""
    patched = bytearray(content)
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        start = 0
        while (position := patched.find(signature, start)) >= 0:
            offset = position + flag_offset
            flags = int.from_bytes(patched[offset : offset + 2], "little") | 1
            patched[offset : offset + 2] = flags.to_bytes(2, "little")
            start = position + len(signature)
    return bytes(patched)


def _run_failed_import(root: Path, content: bytes):
    get_settings().archive_root = str(root)
    with Session(engine) as db:
        _clear(db)
    client = TestClient(app)
    response = client.post(
        "/api/v1/import/chatgpt",
        headers=HEADERS,
        files={"file": ("export.zip", content, "application/zip")},
    )
    assert response.status_code == 202
    job_id = response.json()["import_job_id"]
    assert WorkerRuntime(root, poll_interval=.01, heartbeat_interval=.05, lease_duration=5).run_once()
    result = client.get(f"/api/v1/import-jobs/{job_id}", headers=HEADERS)
    assert result.status_code == 200
    with Session(engine) as db:
        job = db.get(ImportJob, job_id)
        archive = db.get(ArchiveObject, job.archive_object_id)
        assert (root / archive.relative_path).exists()
        assert db.query(Conversation).count() == 0
        assert db.query(Message).count() == 0
        assert db.query(MessageRevision).count() == 0
        assert db.query(MessageObservation).count() == 0
        assert db.query(Attachment).count() == 0
    return job_id, result.json()


def _valid_node(message_id: str, attachments=None):
    return {
        "id": message_id,
        "parent": None,
        "message": {
            "author": {"role": "user"},
            "content": {"parts": ["hello"]},
            "attachments": attachments or [],
        },
    }


def test_encrypted_zip_is_explicitly_unsupported_e2e(tmp_path: Path):
    payload = {"schema_marker": "qingyu-fixture-v1", "conversations": []}
    encrypted = _mark_encrypted(_zip("fixture.json", payload))
    with zipfile.ZipFile(io.BytesIO(encrypted)) as archive:
        assert archive.infolist()[0].flag_bits & 1

    job_id, body = _run_failed_import(tmp_path / "encrypted", encrypted)

    assert body["status"] == "failed"
    assert body["result_code"] == "encrypted_zip_unsupported"
    assert body["error_code"] == "encrypted_zip_unsupported"
    assert body["report"]["errors"] == [{
        "type": "encrypted_zip_unsupported",
        "detail": "encrypted ZIP members are unsupported",
        "severity": "error",
    }]
    assert not list((tmp_path / "encrypted" / "staging" / job_id).glob("*/extracted"))


def test_duplicate_mapping_key_survives_json_and_zip_path_e2e(tmp_path: Path):
    raw = """{
      "schema_marker": "qingyu-fixture-v1",
      "conversations": [{"id":"dup-key","mapping":{
        "node":{"id":"node","parent":null,"message":{"author":{"role":"user"},"content":{"parts":["one"]}}},
        "node":{"id":"node","parent":null,"message":{"author":{"role":"user"},"content":{"parts":["two"]}}}
      }}]
    }"""

    _job_id, body = _run_failed_import(tmp_path / "duplicate-key", _zip("nested/fixture.json", raw))

    assert body["status"] == "failed"
    assert body["result_code"] == "parser_validation_error"
    duplicate = next(error for error in body["report"]["errors"] if error["type"] == "duplicate_json_key")
    assert duplicate["file"] == "nested/fixture.json"
    assert duplicate["raw_pointer"] == "/conversations/0/mapping/node"


def test_duplicate_declared_node_id_is_rejected_e2e(tmp_path: Path):
    payload = {
        "schema_marker": "qingyu-fixture-v1",
        "conversations": [{
            "id": "dup-node",
            "mapping": {
                "first": _valid_node("same"),
                "second": _valid_node("same"),
            },
        }],
    }

    _job_id, body = _run_failed_import(tmp_path / "duplicate-node", _zip("fixture.json", payload))

    assert body["status"] == "failed"
    assert body["result_code"] == "parser_validation_error"
    assert any(error["type"] == "duplicate_node_id" for error in body["report"]["errors"])


def test_duplicate_named_attachment_identity_is_rejected_e2e(tmp_path: Path):
    payload = {
        "schema_marker": "qingyu-fixture-v1",
        "conversations": [{
            "id": "dup-attachment",
            "mapping": {"node": _valid_node("node", [
                {"id": "attachment-1", "filename": "first.bin"},
                {"id": "attachment-1", "filename": "second.bin"},
            ])},
        }],
    }

    _job_id, body = _run_failed_import(tmp_path / "duplicate-attachment", _zip("fixture.json", payload))

    assert body["status"] == "failed"
    assert body["result_code"] == "parser_validation_error"
    duplicate = next(
        error for error in body["report"]["errors"]
        if error["type"] == "duplicate_attachment_identity"
    )
    assert duplicate["raw_pointer"].endswith("/attachments/1")


def test_anonymous_attachment_keys_use_descriptor_hash_and_occurrence_suffix():
    first = {"filename": "one.bin", "media_type": "application/octet-stream", "metadata": {"b": 2, "a": 1}}
    second = {"filename": "two.bin", "media_type": "application/octet-stream"}

    forward = dict(zip(map(canonical_attachment_descriptor, [first, second]), stable_attachment_keys([first, second])))
    reversed_order = dict(zip(map(canonical_attachment_descriptor, [second, first]), stable_attachment_keys([second, first])))

    assert forward == reversed_order
    identical = stable_attachment_keys([first, dict(first)])
    assert identical[0].endswith(":occurrence:1")
    assert identical[1].endswith(":occurrence:2")
