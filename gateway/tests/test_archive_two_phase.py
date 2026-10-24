"""Contract tests for the caller-owned archive transaction API."""

import os
from pathlib import Path

import pytest
from app.archive.service import ArchiveIntegrityError, ArchiveService
from app.models import ArchiveObject, ArchiveObservation
from sqlalchemy import create_engine, delete, event, select
from sqlalchemy.orm import Session

DB_URL = os.environ["TEST_DATABASE_URL"]


@pytest.fixture()
def archive_two_phase_env(tmp_path: Path):
    engine = create_engine(DB_URL)
    ArchiveObject.__table__.create(engine, checkfirst=True)
    ArchiveObservation.__table__.create(engine, checkfirst=True)
    with Session(engine) as db:
        preexisting_observation_ids = set(db.scalars(select(ArchiveObservation.id)))
        try:
            yield tmp_path, db
        finally:
            db.rollback()
            object_ids = list(
                db.scalars(
                    select(ArchiveObservation.archive_object_id).where(
                        ArchiveObservation.id.not_in(preexisting_observation_ids)
                    )
                )
            )
            db.execute(
                delete(ArchiveObservation).where(
                    ArchiveObservation.id.not_in(preexisting_observation_ids)
                )
            )
            db.execute(delete(ArchiveObject).where(ArchiveObject.id.in_(object_ids)))
            db.commit()
    engine.dispose()


def test_prepare_has_no_database_side_effects(archive_two_phase_env):
    root, db = archive_two_phase_env
    source = root / "source.bin"
    source.write_bytes(f"prepared bytes {root}".encode())

    prepared = ArchiveService(root, db).prepare(source, "source.bin", "test")

    assert (root / prepared.relative_path).read_bytes() == source.read_bytes()
    assert db.scalar(select(ArchiveObject).where(ArchiveObject.sha256 == prepared.sha256)) is None


def test_persist_rollback_leaves_only_orphan_bytes(archive_two_phase_env):
    root, db = archive_two_phase_env
    source = root / "source.bin"
    source.write_bytes(f"rollback bytes {root}".encode())
    service = ArchiveService(root, db)
    prepared = service.prepare(source)
    pending = service.persist_prepared(prepared)
    object_id = pending.object_id
    observation_id = pending.observation_id

    db.rollback()

    assert db.get(ArchiveObject, object_id) is None
    assert db.get(ArchiveObservation, observation_id) is None
    assert (root / prepared.relative_path).exists()


def test_finalize_after_commit_writes_manifest(archive_two_phase_env):
    root, db = archive_two_phase_env
    source = root / "source.bin"
    source.write_bytes(f"manifest bytes {root}".encode())
    service = ArchiveService(root, db)
    pending = service.persist_prepared(service.prepare(source))

    db.commit()
    caller_queries = []

    def observe_caller_query(state):
        caller_queries.append(state.statement)

    event.listen(db, "do_orm_execute", observe_caller_query)
    try:
        result = service.finalize(pending)
    finally:
        event.remove(db, "do_orm_execute", observe_caller_query)
    assert result.manifest_path.exists()
    assert caller_queries == []


def test_repeated_prepare_reuses_verified_bytes_without_rows(archive_two_phase_env):
    root, db = archive_two_phase_env
    source = root / "source.bin"
    source.write_bytes(f"same bytes {root}".encode())
    service = ArchiveService(root, db)

    first = service.prepare(source)
    second = service.prepare(source)

    assert first.sha256 == second.sha256
    assert first.duplicate is False
    assert second.duplicate is True
    assert db.scalar(select(ArchiveObject).where(ArchiveObject.sha256 == first.sha256)) is None


def test_persist_adds_exact_observation_without_commit(archive_two_phase_env):
    root, db = archive_two_phase_env
    source = root / "source.bin"
    source.write_bytes(f"observation bytes {root}".encode())
    service = ArchiveService(root, db)
    prepared = service.prepare(
        source,
        "export.bin",
        "fixture",
        "application/x-fixture",
        logical_path="attachments/export.bin",
        observation_metadata={"origin": "message-1"},
    )

    pending = service.persist_prepared(prepared)

    assert pending.observation.archive_object_id == pending.object_id
    assert pending.observation.original_filename == "export.bin"
    assert pending.observation.source_platform == "fixture"
    assert pending.observation.source_path is None
    assert pending.observation.logical_path == "attachments/export.bin"
    assert pending.observation.metadata_json == {
        "origin": "message-1",
        "observed_media_type": "application/x-fixture",
    }
    with Session(db.get_bind()) as observer:
        assert observer.get(ArchiveObject, pending.object_id) is None
        assert observer.get(ArchiveObservation, pending.observation_id) is None


def test_prepare_rejects_corrupted_existing_bytes_without_database_changes(archive_two_phase_env):
    root, db = archive_two_phase_env
    source = root / "source.bin"
    source.write_bytes(f"trusted bytes {root}".encode())
    service = ArchiveService(root, db)
    prepared = service.prepare(source)
    (root / prepared.relative_path).write_bytes(b"corrupt bytes")

    with pytest.raises(ArchiveIntegrityError, match="archive_integrity_failure"):
        service.prepare(source)

    assert db.scalar(select(ArchiveObject).where(ArchiveObject.sha256 == prepared.sha256)) is None


def test_persist_revalidates_bytes_after_prepare(archive_two_phase_env):
    root, db = archive_two_phase_env
    source = root / "source.bin"
    source.write_bytes(f"trusted bytes persist {root}".encode())
    service = ArchiveService(root, db)
    prepared = service.prepare(source)
    (root / prepared.relative_path).write_bytes(b"tampered")

    with pytest.raises(ArchiveIntegrityError, match="archive_integrity_failure"):
        service.persist_prepared(prepared)

    assert db.scalar(select(ArchiveObject).where(ArchiveObject.sha256 == prepared.sha256)) is None


def test_finalize_rejects_rolled_back_observation_for_committed_object(archive_two_phase_env):
    root, db = archive_two_phase_env
    source = root / "source.bin"
    source.write_bytes(f"committed object {root}".encode())
    service = ArchiveService(root, db)
    service.archive_file(source)
    pending = service.persist_prepared(service.prepare(source))
    db.rollback()

    with pytest.raises(RuntimeError, match="observation must be committed"):
        service.finalize(pending)


def test_staging_reparse_is_rejected_before_copy(archive_two_phase_env):
    root, db = archive_two_phase_env
    outside = root.parent / f"{root.name}-outside"
    outside.mkdir()
    try:
        (root / "staging").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    source = root / "source.bin"
    source.write_bytes(b"must not be copied")

    with pytest.raises(ArchiveIntegrityError, match="unsafe archive path"):
        ArchiveService(root, db).prepare(source)

    assert list(outside.iterdir()) == []
