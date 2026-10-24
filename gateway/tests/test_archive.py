import hashlib
import os
from pathlib import Path

import pytest
from app.archive.service import ArchiveService
from app.models import (
    ArchiveObject,
    ArchiveObservation,
    Conversation,
    ImportJob,
    Message,
    MessageObservation,
    MessageRevision,
    SourceAccount,
)
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

DB_URL = os.environ["TEST_DATABASE_URL"]

@pytest.fixture()
def archive_env(tmp_path: Path):
    engine = create_engine(DB_URL)
    ArchiveObject.__table__.create(engine, checkfirst=True)
    ArchiveObservation.__table__.create(engine, checkfirst=True)
    with Session(engine) as db:
        for model in (MessageObservation, MessageRevision, Message, Conversation, ImportJob, ArchiveObservation, SourceAccount, ArchiveObject):
            db.execute(delete(model))
        db.commit()
        yield tmp_path, db
    engine.dispose()

def test_archive_and_idempotent_duplicate(archive_env):
    root, db = archive_env
    src = root / "same.txt"
    src.write_bytes(b"hello archive")
    service = ArchiveService(root, db)
    first = service.archive_file(src, "same.txt", "test")
    second = service.archive_file(src, "same.txt", "test")
    assert first.sha256 == hashlib.sha256(src.read_bytes()).hexdigest()
    assert first.size_bytes == 13
    assert first.duplicate is False and second.duplicate is True
    assert first.object_id == second.object_id
    target = root / first.relative_path
    assert target.read_bytes() == src.read_bytes()
    assert first.manifest_path.exists()

def test_same_name_different_content_and_empty_file(archive_env):
    root, db = archive_env
    service = ArchiveService(root, db)
    a, b = root / "a", root / "b"
    a.write_bytes(b"one")
    b.write_bytes(b"")
    r1 = service.archive_file(a, "same.bin", "test")
    r2 = service.archive_file(b, "same.bin", "test")
    assert r1.sha256 != r2.sha256
    assert r2.size_bytes == 0

def test_large_file_and_content_immutable(archive_env):
    root, db = archive_env
    src = root / "large.bin"
    src.write_bytes(b"x" * (2 * 1024 * 1024))
    result = ArchiveService(root, db).archive_file(src)
    target = root / result.relative_path
    before = target.read_bytes()
    src.write_bytes(b"changed")
    assert target.read_bytes() == before

def test_invalid_names_path_traversal_and_staging_cleanup(archive_env):
    root, db = archive_env
    src = root / "ok.txt"
    src.write_text("x", encoding="utf-8")
    service = ArchiveService(root, db)
    for name in ("../escape.txt", "a/b.txt", "bad?.txt"):
        with pytest.raises(ValueError):
            service.archive_file(src, name)
    stale = service.staging / "stale.tmp"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"stale")
    cleanup = service.cleanup_staging()
    assert stale.resolve() in cleanup.skipped_unknown
    assert stale.exists()

def test_database_and_disk_manifest_match(archive_env):
    import json
    root, db = archive_env
    src = root / "report.json"
    src.write_text("{}", encoding="utf-8")
    result = ArchiveService(root, db).archive_file(src, source_platform="chatgpt", media_type="application/json")
    record = db.get(ArchiveObject, result.object_id)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    target = root / record.relative_path
    assert target.exists()
    assert record.sha256 == manifest["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert record.size_bytes == manifest["size_bytes"] == target.stat().st_size
    assert record.media_type == "application/json"
    assert manifest["integrity_status"] == "verified"
    assert manifest["expected_sha256"] == record.sha256
    assert manifest["integrity_checked_at"] is not None
    assert manifest["integrity_error"] is None

def test_same_object_keeps_multiple_source_observations(archive_env):
    root, db = archive_env
    service = ArchiveService(root, db)
    first = root / "first.txt"
    second = root / "second.txt"
    first.write_bytes(b"same")
    second.write_bytes(b"same")
    a = service.archive_file(first, "export-a.txt", "chatgpt")
    b = service.archive_file(second, "export-b.txt", "telegram")
    rows = db.query(ArchiveObservation).filter_by(archive_object_id=a.object_id).order_by(ArchiveObservation.original_filename).all()
    assert a.object_id == b.object_id
    assert [(r.original_filename, r.source_platform) for r in rows] == [("export-a.txt", "chatgpt"), ("export-b.txt", "telegram")]
    assert len(list((root / "archive" / "objects").rglob(a.sha256))) == 1

def test_manifest_is_content_level_without_source_fields(archive_env):
    import json
    root, db = archive_env
    src = root / "x.txt"
    src.write_bytes(b"manifest")
    result = ArchiveService(root, db).archive_file(src, "name.txt", "manual")
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert "original_filename" not in manifest
    assert "source_platform" not in manifest
    assert manifest["sha256"] == result.sha256



def test_existing_object_digest_corruption_is_rejected(archive_env):
    import json
    root, db = archive_env
    src = root / 'original.bin'; src.write_bytes(b'abc')
    service = ArchiveService(root, db); first = service.archive_file(src, 'original.bin', 'test')
    target = root / first.relative_path; target.write_bytes(b'xyz')
    with pytest.raises(Exception, match='archive_integrity_failure'):
        service.archive_file(src, 'original.bin', 'test')
    assert target.read_bytes() == b'xyz'
    manifest = json.loads(first.manifest_path.read_text(encoding='utf-8'))
    assert manifest['integrity_status'] == 'integrity_failure'
    assert manifest['expected_sha256'] == first.sha256
    assert manifest['integrity_error'].startswith('digest_mismatch:')
    assert manifest['integrity_checked_at'] is not None
    target.write_bytes(b'abc')
    with pytest.raises(Exception, match='explicit reverify required'):
        service.archive_file(src, 'original.bin', 'test')
    assert json.loads(first.manifest_path.read_text(encoding='utf-8'))['integrity_status'] == 'integrity_failure'
