"""Backup, restore, and forward-only upgrade against disposable PostgreSQL databases."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from scripts.maintenance import (
    DUMP_NAME,
    MaintenanceError,
    backup_member,
    can_move_forward,
    canonical_json,
    libpq_env,
    open_target,
    pg_dump_argv,
    pg_restore_argv,
    scrub,
    sha256_bytes,
)
from scripts.maintenance import DatabaseTarget

GATEWAY = Path(__file__).resolve().parents[1]
MARKER = "STAGING-DECOY-MUST-NOT-BE-BACKED-UP"
PAYLOAD = b"qingyu maintenance payload v1\n"


def test_paths_secrets_and_revision_direction_are_refused():
    with pytest.raises(MaintenanceError, match="relative"):
        backup_member("../database.dump")
    with pytest.raises(MaintenanceError, match="staging"):
        backup_member("staging/uploads/decoy.txt")
    with pytest.raises(MaintenanceError, match="not allowed"):
        backup_member("notes.txt")
    assert backup_member("database.dump") == "database.dump"
    assert backup_member("archive/objects/ab/abcdef") == "archive/objects/ab/abcdef"

    secret = "super-secret-pass"
    url = make_url(f"postgresql+psycopg://qingyu:{secret}@127.0.0.1:5432/qingyu_maint_example")
    target = DatabaseTarget(url.render_as_string(hide_password=False), url, url.database, (secret,))
    argv = pg_dump_argv(Path("/tmp/database.dump"))
    restore_argv = pg_restore_argv(Path("/tmp/database.dump"), listing=False, database="qingyu_maint_example")
    assert secret not in " ".join(argv)
    assert secret not in " ".join(restore_argv)
    assert "postgresql" not in " ".join(restore_argv)
    assert restore_argv[restore_argv.index("--dbname") + 1] == "qingyu_maint_example"
    assert "PGPASSWORD" not in " ".join(argv)
    env = libpq_env(target)
    assert env["PGPASSWORD"] == secret
    assert env["PGDATABASE"] == "qingyu_maint_example"
    assert env["PGHOST"] == "127.0.0.1"
    assert secret not in scrub(target.url_text, (secret,))
    assert (
        scrub(f"failed {secret} {target.url_text}", (target.url_text, secret))
        == "failed [redacted] [redacted]"
    )

    with pytest.raises(MaintenanceError, match="does not match --expect-database"):
        open_target(
            "postgresql+psycopg://qingyu:pw@127.0.0.1:1/qingyu_maint_one",
            "qingyu_maint_two",
            operation="backup",
        )
    with pytest.raises(MaintenanceError, match="refusing restore into protected database qingyu"):
        open_target(
            "postgresql+psycopg://qingyu:pw@127.0.0.1:1/qingyu",
            "qingyu",
            operation="restore",
        )

    script = ScriptDirectory(str(GATEWAY / "alembic"))
    head = script.get_heads()[0]
    assert can_move_forward("0002_archive_objects", head)
    assert can_move_forward(head, head)
    assert can_move_forward(None, head)
    assert not can_move_forward(head, "0002_archive_objects")
    assert not can_move_forward("not-a-revision", head)
    left = canonical_json({"b": 1, "a": "清"})
    right = canonical_json({"a": "清", "b": 1})
    assert left == right
    assert left.endswith(b"\n")


def test_cli_rejects_downgrade_and_protected_names_before_connect():
    code, _out, err, leaked = _run(["downgrade", "--expect-database", "qingyu"])
    assert code == 2
    assert "downgrade is not supported" in err
    assert leaked is False

    code, _out, err, leaked = _run(
        ["restore", "--expect-database", "qingyu_test", "--archive-root", "/tmp", "--source", "/tmp"],
        url="postgresql+psycopg://qingyu:pw@127.0.0.1:1/qingyu_maint_unused",
    )
    assert code == 1
    assert "refusing restore into protected database qingyu_test" in err
    assert leaked is False
    assert _revision(os.environ["TEST_DATABASE_URL"]) == _head()


def test_backup_refuses_unsafe_archive_and_wrong_database(tmp_path: Path):
    with _database() as (name, url):
        _alembic(url, "0002_archive_objects")
        archive = tmp_path / "archive"
        _digest, relative = _install_object(url, archive, PAYLOAD)
        before = _revision(url)

        existing = tmp_path / "existing"
        existing.mkdir()
        (existing / "keep.txt").write_text("keep", encoding="utf-8")
        code, _out, err, leaked = _run(
            ["backup", "--expect-database", name, "--archive-root", str(archive), "--dest", str(existing)],
            url=url,
        )
        assert code == 1
        assert "backup destination already exists" in err
        assert leaked is False
        assert (existing / "keep.txt").read_text(encoding="utf-8") == "keep"

        linked = tmp_path / "linked-archive"
        (linked / "archive" / "objects").mkdir(parents=True)
        outside = tmp_path / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        (linked / "archive" / "objects" / "link").symlink_to(outside)
        code, _out, err, _leaked = _run(
            ["backup", "--expect-database", name, "--archive-root", str(linked), "--dest", str(tmp_path / "nope")],
            url=url,
        )
        assert code == 1
        assert "symlink is not allowed" in err
        assert not (tmp_path / "nope").exists()

        mismatched = tmp_path / "mismatched"
        _copy_tree(archive, mismatched)
        (mismatched / relative).write_bytes(b"tampered-bytes")
        dest = tmp_path / "bad-backup"
        code, _out, err, _leaked = _run(
            ["backup", "--expect-database", name, "--archive-root", str(mismatched), "--dest", str(dest)],
            url=url,
        )
        assert code == 1
        assert "do not match the database" in err
        assert not dest.exists()
        assert list(dest.parent.glob(f".{dest.name}.partial-*")) == []

        code, _out, err, _leaked = _run(
            ["upgrade", "--expect-database", "qingyu_maint_other"],
            url=url,
        )
        assert code == 1
        assert "does not match --expect-database" in err
        assert _revision(url) == before == "0002_archive_objects"

        code, _out, err, _leaked = _run(["downgrade"], url=url)
        assert code == 2
        assert _revision(url) == "0002_archive_objects"


def test_backup_restore_and_upgrade_round_trip(tmp_path: Path):
    with _database() as (source_name, source_url):
        _alembic(source_url, "0002_archive_objects")
        archive = tmp_path / "source-archive"
        digest, relative = _install_object(source_url, archive, PAYLOAD)
        decoy = archive / "staging" / "uploads" / "decoy.txt"
        decoy.parent.mkdir(parents=True)
        decoy.write_text(MARKER + "\n", encoding="utf-8")
        (archive / "notes.txt").write_text("not committed\n", encoding="utf-8")
        original_bytes = (archive / relative).read_bytes()

        backup_dir = tmp_path / "pre-upgrade"
        code, _out, err, leaked = _run(
            [
                "upgrade",
                "--expect-database",
                source_name,
                "--archive-root",
                str(archive),
                "--backup-dir",
                str(backup_dir),
            ],
            url=source_url,
        )
        assert code == 0, err
        assert leaked is False
        assert _revision(source_url) == _head()
        assert _object_sha(source_url, digest) == digest
        assert _column(source_url, digest, "original_filename") == "notes.txt"
        assert _column(source_url, digest, "source_platform") == "local"
        assert _column(source_url, digest, "integrity_checked_at") is None
        assert (archive / relative).read_bytes() == original_bytes

        code, out, err, leaked = _run(["upgrade", "--expect-database", source_name], url=source_url)
        assert code == 0, err
        assert leaked is False
        assert f"already at {_head()}" in err
        assert _revision(source_url) == _head()
        assert _object_sha(source_url, digest) == digest

        inventory = json.loads((backup_dir / "inventory.json").read_text(encoding="utf-8"))
        raw_inventory = (backup_dir / "inventory.json").read_text(encoding="utf-8")
        assert inventory["format"] == "qingyu-gateway-backup-v1"
        assert inventory["source_database"] == source_name
        assert inventory["alembic_revision"] == "0002_archive_objects"
        assert inventory["git_commit"] == _git_head()
        assert inventory["postgres_version"].startswith("16.")
        assert "pg_dump (PostgreSQL) 16." in inventory["pg_dump_version"]
        assert "postgresql+" not in raw_inventory.lower()
        assert "127.0.0.1" not in raw_inventory
        assert make_url(source_url).password not in raw_inventory
        assert MARKER not in raw_inventory
        listed = {item["path"] for item in inventory["files"]}
        assert listed == {DUMP_NAME, relative, f"manifests/{digest}.json"}
        assert (backup_dir / "staging").exists() is False
        assert "staging" not in " ".join(listed)
        payload = (backup_dir / "inventory.json").read_bytes()
        hash_line = (backup_dir / "inventory.sha256").read_text(encoding="utf-8")
        assert hash_line == f"{sha256_bytes(payload)}  inventory.json\n"
        assert stat.S_IMODE(backup_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE((backup_dir / DUMP_NAME).stat().st_mode) == 0o600
        assert MARKER.encode() not in (backup_dir / DUMP_NAME).read_bytes()
        assert (backup_dir / relative).read_bytes() == original_bytes

        code, _out, err, _leaked = _run(["verify", "--source", str(backup_dir)])
        assert code == 0, err
        tampered = tmp_path / "tampered"
        _copy_tree(backup_dir, tampered)
        object_path = tampered / relative
        object_path.write_bytes(object_path.read_bytes() + b"x")
        code, _out, err, _leaked = _run(["verify", "--source", str(tampered)])
        assert code == 1
        assert "backup member bytes do not match the inventory" in err

        with _database() as (_empty_name, empty_url):
            empty_archive = tmp_path / "not-empty"
            empty_archive.mkdir()
            (empty_archive / "keep.txt").write_text("keep", encoding="utf-8")
            code, _out, err, _leaked = _run(
                [
                    "restore",
                    "--expect-database",
                    source_name,
                    "--archive-root",
                    str(tmp_path / "unused-root"),
                    "--source",
                    str(backup_dir),
                ],
                url=source_url,
            )
            assert code == 1
            assert "matches the backup source database" in err
            assert _revision(source_url) == _head()

            (tmp_path / "unused-root").mkdir()
            code, _out, err, _leaked = _run(
                [
                    "restore",
                    "--expect-database",
                    make_url(empty_url).database,
                    "--archive-root",
                    str(empty_archive),
                    "--source",
                    str(backup_dir),
                ],
                url=empty_url,
            )
            assert code == 1
            assert "archive root must be an empty directory" in err
            assert (empty_archive / "keep.txt").read_text(encoding="utf-8") == "keep"
            assert _tables(empty_url) == 0

            code, _out, err, _leaked = _run(
                [
                    "restore",
                    "--expect-database",
                    make_url(empty_url).database,
                    "--archive-root",
                    str(tmp_path / "tamper-root"),
                    "--source",
                    str(tampered),
                ],
                url=empty_url,
            )
            assert code == 1
            assert "do not match the inventory" in err
            assert _tables(empty_url) == 0

        with _database() as (restored_name, restored_url):
            restored_root = tmp_path / "restored-archive"
            restored_root.mkdir()
            code, _out, err, leaked = _run(
                [
                    "restore",
                    "--expect-database",
                    restored_name,
                    "--archive-root",
                    str(restored_root),
                    "--source",
                    str(backup_dir),
                ],
                url=restored_url,
            )
            assert code == 0, err
            assert leaked is False
            assert _revision(restored_url) == "0002_archive_objects"
            assert _object_sha(restored_url, digest) == digest
            assert (restored_root / relative).read_bytes() == original_bytes
            assert hashlib.sha256((restored_root / relative).read_bytes()).hexdigest() == digest
            assert stat.S_IMODE((restored_root / relative).stat().st_mode) == 0o644
            assert not (restored_root / "staging").exists()
            assert (restored_root / "manifests" / f"{digest}.json").is_file()
            assert _revision(source_url) == _head()
            assert (archive / relative).read_bytes() == original_bytes
            assert decoy.read_text(encoding="utf-8").startswith(MARKER)

        with _database() as (bad_name, bad_url):
            bad_backup = tmp_path / "rehashed-bad-backup"
            _copy_tree(backup_dir, bad_backup)
            _rewrite_member(bad_backup, relative, b"different-bytes\n")
            bad_root = tmp_path / "bad-restored-archive"
            bad_root.mkdir()
            code, _out, err, _leaked = _run(
                [
                    "restore",
                    "--expect-database",
                    bad_name,
                    "--archive-root",
                    str(bad_root),
                    "--source",
                    str(bad_backup),
                ],
                url=bad_url,
            )
            assert code == 1
            assert "archive object bytes do not match the database" in err
            assert (bad_root / relative).read_bytes() == b"different-bytes\n"


def _run(args: list[str], *, url: str | None = None):
    env = os.environ.copy()
    env["APP_ENV"] = "test"
    secrets: list[str] = []
    if url is not None:
        database = make_url(url).database or ""
        if not database.startswith("qingyu_maint_"):
            raise RuntimeError("refusing maintenance test against a non-disposable database")
        env["DATABASE_URL"] = url
        secrets.append(url)
        password = make_url(url).password
        if password:
            secrets.append(password)
    result = subprocess.run(
        [sys.executable, "-m", "scripts.maintenance", *args],
        cwd=GATEWAY,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    combined = result.stdout + result.stderr
    leaked = any(secret and secret in combined for secret in secrets)
    stdout, stderr = result.stdout, result.stderr
    for secret in secrets:
        stdout = stdout.replace(secret, "[redacted]")
        stderr = stderr.replace(secret, "[redacted]")
    return result.returncode, stdout, stderr, leaked


def _admin_engine():
    admin = make_url(os.environ["TEST_DATABASE_URL"]).set(database="postgres")
    return create_engine(admin, isolation_level="AUTOCOMMIT")


class _database:
    def __enter__(self):
        self.name = f"qingyu_maint_{uuid.uuid4().hex[:16]}"
        if not re.fullmatch(r"qingyu_maint_[0-9a-f]{16}", self.name):
            raise RuntimeError("bad disposable database name")
        self.admin = _admin_engine()
        with self.admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{self.name}"'))
        self.url = make_url(os.environ["TEST_DATABASE_URL"]).set(database=self.name).render_as_string(
            hide_password=False
        )
        return self.name, self.url

    def __exit__(self, exc_type, exc, tb):
        try:
            with self.admin.connect() as conn:
                conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :name AND pid <> pg_backend_pid()"
                    ),
                    {"name": self.name},
                )
                conn.execute(text(f'DROP DATABASE IF EXISTS "{self.name}"'))
        finally:
            self.admin.dispose()
        return False


def _alembic(url: str, revision: str) -> None:
    env = os.environ.copy()
    env["APP_ENV"] = "test"
    env["DATABASE_URL"] = url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=GATEWAY,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    password = make_url(url).password or ""
    stderr = result.stderr.replace(url, "[redacted]").replace(password, "[redacted]")
    assert result.returncode == 0, stderr


def _install_object(url: str, archive_root: Path, payload: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(payload).hexdigest()
    relative = f"archive/objects/{digest[:2]}/{digest}"
    path = archive_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    manifest = archive_root / "manifests" / f"{digest}.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"sha256": digest}) + "\n", encoding="utf-8")
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO archive_objects ("
                    "id, sha256, size_bytes, relative_path, original_filename, "
                    "media_type, source_platform, integrity_status, metadata"
                    ") VALUES ("
                    "CAST(:id AS uuid), :sha256, :size_bytes, :relative_path, :original_filename, "
                    ":media_type, :source_platform, 'verified', CAST(:metadata AS jsonb)"
                    ")"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "sha256": digest,
                    "size_bytes": len(payload),
                    "relative_path": relative,
                    "original_filename": "notes.txt",
                    "media_type": "application/octet-stream",
                    "source_platform": "local",
                    "metadata": "{}",
                },
            )
    finally:
        engine.dispose()
    return digest, relative


def _engine(url: str):
    return create_engine(url)


def _revision(url: str) -> str | None:
    engine = _engine(url)
    try:
        with engine.connect() as conn:
            return conn.scalar(text("SELECT version_num FROM alembic_version"))
    finally:
        engine.dispose()


def _tables(url: str) -> int:
    engine = _engine(url)
    try:
        with engine.connect() as conn:
            return int(
                conn.scalar(
                    text(
                        "SELECT count(*) FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = 'public' AND c.relkind = 'r'"
                    )
                )
            )
    finally:
        engine.dispose()


def _column(url: str, digest: str, column: str):
    if not re.fullmatch(r"[a-z0-9_]+", column):
        raise RuntimeError("bad column")
    engine = _engine(url)
    try:
        with engine.connect() as conn:
            return conn.scalar(
                text(f"SELECT {column} FROM archive_objects WHERE sha256 = :sha"),
                {"sha": digest},
            )
    finally:
        engine.dispose()


def _object_sha(url: str, digest: str) -> str | None:
    return _column(url, digest, "sha256")


def _head() -> str:
    heads = ScriptDirectory(str(GATEWAY / "alembic")).get_heads()
    assert len(heads) == 1
    return heads[0]


def _git_head() -> str:
    result = subprocess.run(
        ["git", "-C", str(GATEWAY.parent), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _copy_tree(source: Path, dest: Path) -> None:
    shutil.copytree(source, dest)


def _rewrite_member(backup: Path, relative: str, new_bytes: bytes) -> None:
    path = backup / relative
    path.write_bytes(new_bytes)
    inventory = json.loads((backup / "inventory.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256(new_bytes).hexdigest()
    for item in inventory["files"]:
        if item["path"] == relative:
            item["sha256"] = digest
            item["size_bytes"] = len(new_bytes)
    archive_items = [item for item in inventory["files"] if item["path"] != DUMP_NAME]
    inventory["archive_file_count"] = len(archive_items)
    inventory["archive_total_bytes"] = sum(item["size_bytes"] for item in archive_items)
    payload = canonical_json(inventory)
    (backup / "inventory.json").write_bytes(payload)
    (backup / "inventory.sha256").write_text(f"{sha256_bytes(payload)}  inventory.json\n", encoding="utf-8")
