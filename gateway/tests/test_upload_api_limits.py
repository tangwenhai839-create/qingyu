import io
from pathlib import Path

import pytest
from app.api_import import _write_upload_with_limit
from app.archive.service import _sha256_file as archive_sha256_file
from app.config import Settings, get_settings
from app.db import get_db
from app.importers.zip_staging import _sha256_file as zip_sha256_file
from app.main import app
from app.request_limits import MULTIPART_OVERHEAD_BYTES
from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient
from pydantic import ValidationError

HEADERS = {"Authorization": "Bearer qingyu-test-token-only"}


def _upload(content: bytes) -> UploadFile:
    return UploadFile(io.BytesIO(content), filename="export.zip")


def test_upload_exactly_at_limit_is_allowed(tmp_path: Path):
    destination = tmp_path / "upload.zip"

    _write_upload_with_limit(_upload(b"1234"), destination, 4)

    assert destination.read_bytes() == b"1234"


def test_upload_one_byte_over_limit_is_rejected(tmp_path: Path):
    destination = tmp_path / "upload.zip"

    with pytest.raises(HTTPException) as error:
        _write_upload_with_limit(_upload(b"12345"), destination, 4)

    assert error.value.status_code == 413


def test_oversized_import_returns_413_and_removes_temporary_file(tmp_path: Path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "archive_root", str(tmp_path))
    monkeypatch.setattr(settings, "import_upload_max_bytes", 4)
    app.dependency_overrides[get_db] = lambda: None
    try:
        response = TestClient(app).post(
            "/api/v1/import/chatgpt",
            headers=HEADERS,
            files={"file": ("export.zip", b"12345", "application/zip")},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 413
    assert response.json()["detail"] == "上传文件超过 4 字节限制"
    assert not [path for path in (tmp_path / "staging" / "uploads").iterdir() if path.is_dir()]


def test_oversized_archive_returns_413_and_removes_temporary_file(tmp_path: Path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "archive_root", str(tmp_path))
    monkeypatch.setattr(settings, "import_upload_max_bytes", 4)
    app.dependency_overrides[get_db] = lambda: None
    try:
        response = TestClient(app).post(
            "/api/v1/archive/objects",
            headers=HEADERS,
            files={"file": ("evidence.bin", b"12345", "application/octet-stream")},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 413
    assert response.json()["detail"] == "上传文件超过 4 字节限制"
    assert not [path for path in (tmp_path / "staging" / "uploads").iterdir() if path.is_dir()]


@pytest.mark.parametrize("path", ["/api/v1/import/chatgpt", "/api/v1/archive/objects"])
def test_request_body_limit_rejects_before_route_or_multipart_spooling(monkeypatch, path: str):
    settings = get_settings()
    monkeypatch.setattr(settings, "import_upload_max_bytes", 4)
    route_called = False

    async def downstream(_scope, _receive, _send):
        nonlocal route_called
        route_called = True

    from app.request_limits import ImportRequestSizeLimitMiddleware

    middleware = ImportRequestSizeLimitMiddleware(downstream)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"x", "more_body": False}

    async def send(message):
        sent.append(message)

    import asyncio

    asyncio.run(middleware(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(b"content-length", str(5 + MULTIPART_OVERHEAD_BYTES).encode())],
        },
        receive,
        send,
    ))

    assert route_called is False
    assert sent[0]["status"] == 413


@pytest.mark.parametrize("path", ["/api/v1/import/chatgpt", "/api/v1/archive/objects"])
def test_chunked_request_body_is_limited_without_content_length(monkeypatch, path: str):
    settings = get_settings()
    monkeypatch.setattr(settings, "import_upload_max_bytes", 4)
    limit = 4 + MULTIPART_OVERHEAD_BYTES
    messages = iter([
        {"type": "http.request", "body": b"x" * limit, "more_body": True},
        {"type": "http.request", "body": b"x", "more_body": False},
    ])
    sent = []

    async def downstream(_scope, receive, _send):
        await receive()
        await receive()

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    import asyncio

    from app.request_limits import ImportRequestSizeLimitMiddleware

    asyncio.run(ImportRequestSizeLimitMiddleware(downstream)(
        {"type": "http", "method": "POST", "path": path, "headers": []},
        receive,
        send,
    ))

    assert sent[0]["status"] == 413


def test_upload_limit_must_be_positive():
    with pytest.raises(ValidationError):
        Settings(app_env="test", import_upload_max_bytes=0, _env_file=None)


def test_file_digest_helpers_stream_without_path_read_bytes(tmp_path: Path, monkeypatch):
    source = tmp_path / "payload.bin"
    source.write_bytes(b"streamed digest")
    expected = "f763d6929545baa708370269fd9d44877ea647da39977076e38f49ad9b6b0307"

    def fail_read_bytes(_self):
        raise AssertionError("Path.read_bytes must not be used for hashing")

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)

    assert archive_sha256_file(source) == expected
    assert zip_sha256_file(source) == expected


@pytest.mark.parametrize("suffix", ["", "/retry"])
def test_import_job_routes_reject_invalid_uuid(suffix: str):
    app.dependency_overrides[get_db] = lambda: None
    try:
        client = TestClient(app)
        response = (
            client.get(f"/api/v1/import-jobs/not-a-uuid{suffix}", headers=HEADERS)
            if not suffix
            else client.post(f"/api/v1/import-jobs/not-a-uuid{suffix}", headers=HEADERS)
        )
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 422
