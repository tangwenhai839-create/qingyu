import uuid
from pathlib import Path
from types import SimpleNamespace

from app.archive.service import ArchiveService
from app.config import get_settings
from app.db import get_db
from app.main import app
from fastapi.testclient import TestClient

HEADERS = {"Authorization": "Bearer qingyu-test-token-only"}


def test_archive_response_and_openapi_do_not_expose_filesystem_paths(tmp_path: Path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "archive_root", str(tmp_path))
    object_id = uuid.uuid4()
    internal_path = str(tmp_path / "archive" / "objects" / "private")
    manifest_path = tmp_path / "manifests" / "private.json"

    def fake_archive(*_args, **_kwargs):
        return SimpleNamespace(
            object_id=object_id,
            sha256="a" * 64,
            size_bytes=7,
            duplicate=False,
            relative_path="archive/objects/private",
            manifest_path=manifest_path,
        )

    monkeypatch.setattr(ArchiveService, "archive_file", fake_archive)
    app.dependency_overrides[get_db] = lambda: object()
    try:
        client = TestClient(app)
        response = client.post(
            "/api/v1/archive/objects",
            headers=HEADERS,
            files={"file": ("evidence.bin", b"evidence", "application/octet-stream")},
        )
        schema = client.get("/openapi.json").json()
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 201
    assert response.json() == {
        "object_id": str(object_id),
        "sha256": "a" * 64,
        "size_bytes": 7,
        "duplicate": False,
    }
    assert internal_path not in response.text
    assert str(manifest_path) not in response.text
    assert "archive/objects" not in response.text
    response_schema = schema["components"]["schemas"]["ArchiveObjectResponse"]
    assert set(response_schema["properties"]) == {
        "object_id",
        "sha256",
        "size_bytes",
        "duplicate",
    }
