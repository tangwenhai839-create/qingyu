import os
import threading
import time
import uuid
from pathlib import Path

import app.importers.cleanup as cleanup_module
import app.importers.staging as staging_module
import pytest
from app.db import engine
from app.importers.cleanup import (
    StagingCleanupResult,
    _quarantine_and_remove,
    cleanup_staging,
    cleanup_terminal_attempt,
)
from app.importers.staging import (
    UnsafeStagingRoot,
    create_prejob_generation,
    ensure_staging_layout,
    generation_is_active,
    job_attempt_path,
    safe_remove_staging,
)
from app.models import ArchiveObject, ImportJob, SourceAccount
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session


class NoDatabase:
    """Pre-job-only scans never need a DB connection."""

    def scalar(self, *_args, **_kwargs):
        raise AssertionError("pre-job cleanup must not query ImportJob")


def _age(path: Path, days: int = 10):
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))


def test_layout_and_attempt_paths_are_strictly_namespaced(tmp_path):
    layout = ensure_staging_layout(tmp_path)
    assert {item.name for item in layout.staging.iterdir()} == {
        "uploads", "archive-prepared", "jobs", "quarantine"
    }
    job_id, token = uuid.uuid4(), uuid.uuid4()
    attempt = job_attempt_path(tmp_path, job_id, 2, token, create=True)
    assert attempt == layout.jobs / str(job_id) / "2" / str(token)
    with pytest.raises(ValueError):
        job_attempt_path(tmp_path, job_id, -1, token)


@pytest.mark.parametrize("namespace", ["uploads", "archive-prepared"])
def test_live_prejob_os_lock_protects_then_abandoned_generation_expires(tmp_path, namespace):
    generation = create_prejob_generation(tmp_path, namespace)
    generation.payload_path().write_bytes(b"payload")
    _age(generation.directory)
    assert generation_is_active(generation.directory) is True

    active = cleanup_staging(tmp_path, NoDatabase(), retention_days=7)
    assert active.skipped_active == [generation.directory.resolve()]
    assert generation.directory.exists()

    generation.close()
    expired = cleanup_staging(tmp_path, NoDatabase(), retention_days=7)
    assert expired.deleted == [generation.directory.resolve()]
    assert not generation.directory.exists()


def test_recent_abandoned_prejob_generation_obeys_retention(tmp_path):
    generation = create_prejob_generation(tmp_path, "uploads")
    path = generation.directory.resolve()
    generation.close()
    result = cleanup_staging(tmp_path, NoDatabase(), retention_days=7)
    assert result.skipped_retention == [path]
    assert result.skipped_recent is result.skipped_retention
    assert path.exists()


def test_quarantine_left_by_crash_is_recognized_and_recovered(tmp_path):
    layout = ensure_staging_layout(tmp_path)
    abandoned = layout.quarantine / str(uuid.uuid4())
    abandoned.mkdir()
    (abandoned / "payload").write_text("x")
    result = cleanup_staging(tmp_path, NoDatabase())
    assert result.quarantine_recovered == [abandoned.resolve()]
    assert not abandoned.exists()
    assert cleanup_staging(tmp_path, NoDatabase()).quarantine_recovered == []


def test_safe_delete_rejects_root_outside_and_links(tmp_path):
    layout = ensure_staging_layout(tmp_path / "root")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("preserve")
    with pytest.raises(UnsafeStagingRoot):
        safe_remove_staging(layout.staging, layout.root)
    with pytest.raises(UnsafeStagingRoot):
        safe_remove_staging(outside, layout.root)

    candidate = layout.uploads / str(uuid.uuid4())
    candidate.mkdir()
    try:
        (candidate / "redirect").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc.__class__.__name__}")
    with pytest.raises(UnsafeStagingRoot):
        safe_remove_staging(candidate, layout.root)
    assert sentinel.read_text() == "preserve"


def test_safe_delete_failure_remains_in_formal_quarantine(tmp_path, monkeypatch):
    layout = ensure_staging_layout(tmp_path)
    candidate = layout.uploads / str(uuid.uuid4())
    candidate.mkdir()
    (candidate / "payload").write_text("x")
    monkeypatch.setattr(
        "app.importers.cleanup._remove_tree_without_links",
        lambda _path: (_ for _ in ()).throw(PermissionError("busy")),
    )
    result = safe_remove_staging(candidate, tmp_path)
    assert result.status == "quarantined"
    assert result.path.parent == layout.quarantine
    assert result.path.exists()
    assert not candidate.exists()


def test_quarantine_rename_uses_formal_namespace(tmp_path, monkeypatch):
    layout = ensure_staging_layout(tmp_path)
    candidate = layout.uploads / str(uuid.uuid4())
    candidate.mkdir()
    (candidate / "payload").write_text("x")
    destinations = []
    original = os.replace

    def observe(source, destination):
        destinations.append(Path(destination))
        return original(source, destination)

    monkeypatch.setattr("app.importers.cleanup.os.replace", observe)
    _quarantine_and_remove(candidate, layout.staging)
    assert destinations[0].parent == layout.quarantine
    uuid.UUID(destinations[0].name)
    assert not candidate.exists()


def test_result_exposes_complete_observable_outcomes():
    result = StagingCleanupResult()
    result.record("skipped_retention", Path("recent"))
    result.record("quarantined", Path("quarantined"))
    result.record("quarantine_recovered", Path("recovered"))
    assert result.counts == {
        "deleted": 0,
        "skipped_active": 0,
        "skipped_retention": 1,
        "skipped_recent": 1,
        "skipped_unknown": 0,
        "quarantined": 1,
        "quarantine_recovered": 1,
        "failed": 0,
    }


def test_max_candidates_bounds_one_cleanup_pass(tmp_path):
    generations = []
    for _ in range(3):
        generation = create_prejob_generation(tmp_path, "uploads")
        generation.close()
        _age(generation.directory)
        generations.append(generation.directory)
    result = cleanup_staging(tmp_path, NoDatabase(), retention_days=7, max_candidates=1)
    assert len(result.deleted) == 1
    assert sum(path.exists() for path in generations) == 2
    with pytest.raises(ValueError, match="max_candidates"):
        cleanup_staging(tmp_path, NoDatabase(), max_candidates=0)


def test_rotation_prevents_namespace_starvation_with_one_candidate_budget(tmp_path):
    layout = ensure_staging_layout(tmp_path)
    quarantine = layout.quarantine / str(uuid.uuid4())
    quarantine.mkdir(); (quarantine / "x").write_text("x")
    generations = []
    for namespace in ("uploads", "archive-prepared"):
        generation = create_prejob_generation(tmp_path, namespace)
        generation.close(); _age(generation.directory)
        generations.append(generation.directory)
    for _ in range(4):
        cleanup_staging(tmp_path, NoDatabase(), retention_days=7, max_candidates=1)
    assert not quarantine.exists()
    assert all(not path.exists() for path in generations)


def test_queue_cursor_advances_past_active_head_to_expired_tail(tmp_path):
    active = create_prejob_generation(tmp_path, "uploads")
    _age(active.directory)
    expired = create_prejob_generation(tmp_path, "uploads")
    expired_path = expired.directory
    expired.close(); _age(expired_path)

    outcomes = [cleanup_staging(tmp_path, NoDatabase(), 7, max_candidates=1) for _ in range(6)]

    assert active.directory.exists()
    assert not expired_path.exists()
    assert any(active.directory.resolve() in result.skipped_active for result in outcomes)
    active.close(remove=True)


def test_queue_intent_precedes_generation_visibility(tmp_path, monkeypatch):
    ensure_staging_layout(tmp_path)
    original = Path.mkdir
    failed_once = False

    def crash_before_generation_mkdir(path, *args, **kwargs):
        nonlocal failed_once
        if path.parent.name == "uploads" and not path.name.startswith(".") and not failed_once:
            failed_once = True
            raise OSError("simulated hard crash boundary")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", crash_before_generation_mkdir)
    with pytest.raises(OSError):
        create_prejob_generation(tmp_path, "uploads")
    monkeypatch.setattr(Path, "mkdir", original)
    generation = create_prejob_generation(tmp_path, "uploads")
    generation.close(); _age(generation.directory)

    for _ in range(3):
        cleanup_staging(tmp_path, NoDatabase(), 7, max_candidates=1)
    assert not generation.directory.exists()


def test_file_entries_count_against_scan_and_delete_budget(tmp_path):
    layout = ensure_staging_layout(tmp_path)
    candidate = layout.uploads / str(uuid.uuid4())
    candidate.mkdir()
    for index in range(3):
        (candidate / f"{index}.bin").write_bytes(b"x")
    with pytest.raises(cleanup_module.CleanupBudgetExhausted):
        cleanup_module._tree_is_safe(candidate, layout.staging, max_nodes=2)

    quarantine = layout.quarantine / str(uuid.uuid4())
    candidate.rename(quarantine)
    with pytest.raises(cleanup_module.CleanupBudgetExhausted):
        cleanup_module._remove_tree_without_links(quarantine, {"remaining": 2})
    assert quarantine.exists()


def test_large_tree_budget_exhaustion_retries_from_quarantine(tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup_module, "TREE_ENTRY_BUDGET", 3)
    generation = create_prejob_generation(tmp_path, "uploads")
    for index in range(6):
        generation.payload_path(f"{index}.bin").write_bytes(b"x")
    original = generation.directory
    generation.close(); _age(original)

    first = cleanup_staging(tmp_path, NoDatabase(), 7)
    assert len(first.quarantined) == 1
    assert not original.exists()
    for _ in range(4):
        cleanup_staging(tmp_path, NoDatabase(), 7)
    assert not first.quarantined[0].exists()


def test_close_isolates_unlock_and_internal_cleanup_errors(tmp_path, monkeypatch):
    generation = create_prejob_generation(tmp_path, "uploads")
    original_unlock = staging_module._unlock
    monkeypatch.setattr("app.importers.staging._unlock", lambda _owner: (_ for _ in ()).throw(OSError("busy")))
    result = generation.close(remove=True)
    assert result.status == "failed"

    monkeypatch.setattr("app.importers.staging._unlock", original_unlock)
    second = create_prejob_generation(tmp_path, "uploads")
    monkeypatch.setattr("app.importers.staging.safe_remove_staging", lambda *_args: (_ for _ in ()).throw(RuntimeError("race")))
    assert second.close(remove=True).status == "failed"


def test_creator_scanner_race_at_retention_zero_keeps_published_owner_active(tmp_path, monkeypatch):
    entered = threading.Event(); release = threading.Event()
    original = Path.write_bytes

    def paused_write(path, data):
        if path.name == ".owner.lock":
            entered.set(); assert release.wait(3)
        return original(path, data)

    monkeypatch.setattr(Path, "write_bytes", paused_write)
    created = []
    creator = threading.Thread(target=lambda: created.append(create_prejob_generation(tmp_path, "uploads")))
    creator.start(); assert entered.wait(3)
    scanned = []
    scanner = threading.Thread(
        target=lambda: scanned.append(cleanup_staging(tmp_path, NoDatabase(), retention_days=0))
    )
    scanner.start(); release.set()
    creator.join(3); scanner.join(3)
    assert not creator.is_alive() and not scanner.is_alive()
    assert created and created[0].directory.exists()
    assert created[0].directory.resolve() in scanned[0].skipped_active
    created[0].close(remove=True)


def test_archive_and_staging_symlinks_fail_closed_without_touching_sentinel(tmp_path):
    outside = tmp_path / "outside"; outside.mkdir()
    sentinel = outside / "sentinel"; sentinel.write_text("preserve")
    archive_link = tmp_path / "archive-link"
    try:
        archive_link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc.__class__.__name__}")
    with pytest.raises(UnsafeStagingRoot):
        ensure_staging_layout(archive_link)
    assert not (outside / "staging").exists()

    root = tmp_path / "root"; root.mkdir()
    (root / "staging").symlink_to(outside, target_is_directory=True)
    with pytest.raises(UnsafeStagingRoot):
        ensure_staging_layout(root)
    result = cleanup_staging(root, NoDatabase(), retention_days=0)
    assert result.skipped_unknown == [(root / "staging").absolute()]
    assert sentinel.read_text() == "preserve"


def test_scandir_failure_is_observable(tmp_path, monkeypatch):
    layout = ensure_staging_layout(tmp_path)
    original = os.scandir

    def fail_uploads(path):
        if Path(path) == layout.uploads:
            raise PermissionError("denied")
        return original(path)

    monkeypatch.setattr(os, "scandir", fail_uploads)
    result = cleanup_staging(tmp_path, NoDatabase())
    assert layout.uploads.resolve() in result.failed


def test_nested_scandir_failure_is_observable(tmp_path, monkeypatch):
    generation = create_prejob_generation(tmp_path, "uploads")
    nested = generation.directory / "nested"
    nested.mkdir(); (nested / "payload").write_text("x")
    generation.close(); _age(generation.directory)
    original = os.scandir

    def fail_nested(path):
        if Path(path).resolve() == nested.resolve():
            raise PermissionError("denied")
        return original(path)

    monkeypatch.setattr(os, "scandir", fail_nested)
    result = cleanup_staging(tmp_path, NoDatabase(), 7)
    assert generation.directory.resolve() in result.failed
    assert generation.directory.exists()


@pytest.fixture
def lifecycle_job():
    with Session(engine, expire_on_commit=False) as db:
        assert db.scalar(select(func.current_database())) == "qingyu_test"
        account = SourceAccount(platform="staging-lifecycle", account_label=str(uuid.uuid4()), metadata_json={})
        archive = ArchiveObject(
            sha256=uuid.uuid4().hex * 2,
            size_bytes=1,
            relative_path="unused",
            media_type="application/zip",
            integrity_status="verified",
            metadata_json={},
        )
        db.add_all([account, archive])
        db.flush()
        job = ImportJob(
            source_account_id=account.id,
            archive_object_id=archive.id,
            parser_name="fixture",
            parser_version="1",
            status="completed",
            attempt_count=1,
            report_json={},
        )
        db.add(job)
        db.commit()
        ids = job.id, account.id, archive.id
        yield ids
        db.rollback()
        db.execute(delete(ImportJob).where(ImportJob.id == ids[0]))
        db.execute(delete(SourceAccount).where(SourceAccount.id == ids[1]))
        db.execute(delete(ArchiveObject).where(ArchiveObject.id == ids[2]))
        db.commit()


def _old_attempt(root, job_id, token):
    path = job_attempt_path(root, job_id, 1, token, create=True)
    (path / "payload").write_text("x")
    _age(path)
    return path.resolve()


def test_attempt_cleanup_final_row_lock_revalidates_state(tmp_path, lifecycle_job, monkeypatch):
    job_id, _, _ = lifecycle_job
    token = uuid.uuid4()
    path = _old_attempt(tmp_path, job_id, token)
    original = cleanup_module._classify_attempt
    changed = False

    def flip_after_preflight(db, selected_job, attempt, selected_token):
        nonlocal changed
        outcome = original(db, selected_job, attempt, selected_token)
        if not changed:
            changed = True
            with Session(engine) as update_db:
                current = update_db.get(ImportJob, job_id)
                current.status = "pending"
                update_db.commit()
        return outcome

    monkeypatch.setattr(cleanup_module, "_classify_attempt", flip_after_preflight)
    with Session(engine) as db:
        result = cleanup_staging(tmp_path, db, retention_days=7)
    assert result.skipped_active == [path]
    assert path.exists()


def test_terminal_attempt_immediate_cleanup_ignores_retention(tmp_path, lifecycle_job):
    job_id, _, _ = lifecycle_job
    token = uuid.uuid4()
    path = job_attempt_path(tmp_path, job_id, 1, token, create=True)
    (path / "payload").write_text("x")
    with Session(engine) as db:
        result = cleanup_terminal_attempt(tmp_path, db, job_id, 1, token)
    assert result.outcome == "deleted"
    assert not path.exists()


def test_large_terminal_attempt_is_quarantined_then_incrementally_removed(
    tmp_path, lifecycle_job, monkeypatch
):
    monkeypatch.setattr(cleanup_module, "TREE_ENTRY_BUDGET", 3)
    job_id, _, _ = lifecycle_job
    token = uuid.uuid4()
    path = job_attempt_path(tmp_path, job_id, 1, token, create=True)
    for index in range(6):
        (path / f"{index}.bin").write_bytes(b"x")
    with Session(engine) as db:
        result = cleanup_terminal_attempt(tmp_path, db, job_id, 1, token)
        assert result.outcome == "quarantined"
        assert not path.exists() and result.path.exists()
        for _ in range(4):
            cleanup_staging(tmp_path, db, retention_days=0)
    assert not result.path.exists()


def test_two_scanners_racing_one_orphan_are_idempotent(tmp_path, monkeypatch):
    job_id, token = uuid.uuid4(), uuid.uuid4()
    path = _old_attempt(tmp_path, job_id, token)
    barrier = threading.Barrier(2)
    original = cleanup_module._move_to_quarantine

    def synchronized_move(candidate, base):
        if candidate == path:
            barrier.wait(timeout=3)
        return original(candidate, base)

    monkeypatch.setattr(cleanup_module, "_move_to_quarantine", synchronized_move)
    results = []

    def scan():
        with Session(engine) as db:
            results.append(cleanup_staging(tmp_path, db, retention_days=7))

    threads = [threading.Thread(target=scan), threading.Thread(target=scan)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    assert not path.exists()
    assert sum(len(result.deleted) for result in results) == 1
    assert all(not result.failed for result in results)
