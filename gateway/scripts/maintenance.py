"""Backup, restore, and forward-only upgrade for one Gateway database.

The backup is a logical ``pg_dump`` custom archive plus the committed
``archive/objects`` and ``manifests`` trees. ``staging/`` is not copied.
Passwords are placed only in a child process environment, never in argv, and
error text is scrubbed before it is printed.

Restore refuses the long-term application database, the test database, and the
PostgreSQL service databases. It also refuses to restore into the database
named by the backup. Upgrade never downgrades.

``repair-manifests`` fills manifest files that are missing. It does not
overwrite a manifest that is already a regular file. ``verify --evidence``
checks observation links, object bytes, and manifest completeness against a
live database and archive root.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError

FORMAT = "qingyu-gateway-backup-v1"
DUMP_NAME = "database.dump"
INVENTORY_NAME = "inventory.json"
INVENTORY_HASH_NAME = "inventory.sha256"
COMMITTED_PREFIXES = ("archive/objects/", "manifests/")
# (table, nullable archive_object_id). Names are fixed; they are not user input.
EVIDENCE_LINK_TABLES = (
    ("archive_observations", False),
    ("conversation_observations", False),
    ("attachment_observations", True),
    ("message_revisions", True),
)
MANIFEST_REQUIRED_KEYS = (
    "manifest_version",
    "object_id",
    "sha256",
    "expected_sha256",
    "size_bytes",
    "relative_path",
    "media_type",
    "integrity_status",
    "integrity_checked_at",
    "integrity_error",
)
PROTECTED_RESTORE_DATABASES = frozenset(
    {"qingyu", "qingyu_test", "postgres", "template0", "template1"}
)
FORBIDDEN_URL_QUERY_KEYS = frozenset(
    {"dbname", "database", "service", "servicefile", "options", "host", "port", "user", "password", "passfile"}
)
GATEWAY_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = GATEWAY_DIR.parent


class MaintenanceError(RuntimeError):
    """A failure whose message is safe to print."""


@dataclass(frozen=True)
class DatabaseTarget:
    url_text: str
    url: URL
    name: str
    secrets: tuple[str, ...]


def scrub(text: str, secrets: tuple[str, ...]) -> str:
    cleaned = text
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _open_nofollow(path: Path) -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise MaintenanceError("this platform cannot refuse symlinks")
    try:
        return os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise MaintenanceError("symlink is not allowed in the archive") from None
        raise MaintenanceError("archive file could not be read") from None


def hash_regular_file(path: Path) -> tuple[int, str]:
    """Hash a regular file without following a symlink or loading it all at once."""
    fd = _open_nofollow(path)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise MaintenanceError("archive entry is not a regular file")
        digest = hashlib.sha256()
        size = 0
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            digest.update(block)
            size += len(block)
        if size != info.st_size:
            raise MaintenanceError("archive file changed while reading")
        return size, digest.hexdigest()
    finally:
        os.close(fd)


def _copy_regular_file(source: Path, target: Path, mode: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    in_fd = _open_nofollow(source)
    out_fd = -1
    try:
        info = os.fstat(in_fd)
        if not stat.S_ISREG(info.st_mode):
            raise MaintenanceError("archive entry is not a regular file")
        out_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        while True:
            block = os.read(in_fd, 1024 * 1024)
            if not block:
                break
            os.write(out_fd, block)
        os.fsync(out_fd)
    finally:
        os.close(in_fd)
        if out_fd >= 0:
            os.close(out_fd)
    os.chmod(target, mode)


def _relative_parts(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or value.startswith(("/", "\\")) or "\\" in value:
        raise MaintenanceError("path is not a relative archive path")
    parts = Path(value).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise MaintenanceError("path is not a relative archive path")
    return parts


def committed_relative(value: str) -> str:
    parts = _relative_parts(value)
    if "staging" in parts:
        raise MaintenanceError("staging is excluded from backups")
    relative = Path(*parts).as_posix()
    if not relative.startswith("archive/objects/"):
        raise MaintenanceError("archive object path is outside archive/objects")
    return relative


def backup_member(value: str) -> str:
    parts = _relative_parts(value)
    if "staging" in parts:
        raise MaintenanceError("staging is excluded from backups")
    relative = Path(*parts).as_posix()
    if relative == DUMP_NAME or relative.startswith(COMMITTED_PREFIXES):
        return relative
    raise MaintenanceError("backup member path is not allowed")


def require_database_url(explicit: str | None) -> str:
    value = explicit if explicit is not None else os.environ.get("DATABASE_URL")
    if value is None or not value.strip():
        raise MaintenanceError("DATABASE_URL is required")
    return value


def open_target(database_url: str, expect_database: str, *, operation: str) -> DatabaseTarget:
    if not expect_database or expect_database != expect_database.strip():
        raise MaintenanceError("database name does not match --expect-database")
    if operation == "restore" and expect_database.lower() in PROTECTED_RESTORE_DATABASES:
        raise MaintenanceError(f"refusing restore into protected database {expect_database}")
    try:
        url = make_url(database_url)
    except ArgumentError:
        raise MaintenanceError("database_url is invalid") from None
    if url.drivername not in {"postgresql", "postgresql+psycopg"}:
        raise MaintenanceError("database_url must be a PostgreSQL URL")
    forbidden = FORBIDDEN_URL_QUERY_KEYS.intersection(key.lower() for key in url.query)
    if forbidden:
        raise MaintenanceError("database_url contains a forbidden connection override")
    if url.database != expect_database:
        raise MaintenanceError("database name does not match --expect-database")
    if not url.host or url.port is None:
        raise MaintenanceError("database_url host and port are required")
    if not url.username:
        raise MaintenanceError("database_url user is required")
    if not url.password:
        raise MaintenanceError("database_url password is required")
    secrets = tuple(
        item
        for item in (url.password, database_url, url.render_as_string(hide_password=False))
        if item
    )
    engine = create_engine(database_url, pool_pre_ping=False)
    try:
        try:
            with engine.connect() as conn:
                current = conn.scalar(text("SELECT current_database()"))
        except SQLAlchemyError as exc:
            raise MaintenanceError(f"database connection failed ({exc.__class__.__name__})") from None
    finally:
        engine.dispose()
    if current != expect_database:
        raise MaintenanceError("connected database does not match --expect-database")
    return DatabaseTarget(database_url, url, expect_database, secrets)


def libpq_env(target: DatabaseTarget) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PGHOST": target.url.host or "",
        "PGPORT": str(target.url.port),
        "PGUSER": target.url.username or "",
        "PGDATABASE": target.name,
        "PGPASSWORD": target.url.password or "",
        "PGPASSFILE": os.devnull,
        "PGAPPNAME": "qingyu-maintenance",
    }
    sslmode = target.url.query.get("sslmode")
    if isinstance(sslmode, str) and sslmode:
        env["PGSSLMODE"] = sslmode
    return env


def pg_dump_argv(dump_path: Path) -> list[str]:
    return [
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--serializable-deferrable",
        "--file",
        str(dump_path),
    ]


def pg_restore_argv(dump_path: Path, *, listing: bool, database: str | None = None) -> list[str]:
    if listing:
        return ["pg_restore", "--list", str(dump_path)]
    if not database or database.startswith("-") or "/" in database:
        raise MaintenanceError("restore database name is invalid")
    return [
        "pg_restore",
        "--dbname",
        database,
        "--no-owner",
        "--no-privileges",
        "--exit-on-error",
        "--single-transaction",
        str(dump_path),
    ]


def _run(
    argv: list[str], *, env: dict[str, str], secrets: tuple[str, ...], cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    if shutil.which(argv[0]) is None:
        raise MaintenanceError(f"{argv[0]} is not available")
    joined = " ".join(argv)
    for secret in secrets:
        if secret and secret in joined:
            raise MaintenanceError("refusing to put a secret on a command line")
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        raise MaintenanceError(f"{argv[0]} could not be started") from None
    if result.returncode != 0:
        detail = scrub(result.stderr.strip() or result.stdout.strip(), secrets)
        raise MaintenanceError(f"{argv[0]} failed: {detail}")
    return result


def tool_version(name: str) -> str:
    if shutil.which(name) is None:
        raise MaintenanceError(f"{name} is not available")
    result = subprocess.run([name, "--version"], text=True, capture_output=True, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        raise MaintenanceError(f"{name} is not available")
    return result.stdout.strip().splitlines()[0]


def git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_DIR), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    if result.returncode != 0 or len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        return None
    return value


def _engine(url: str, *, read_only: bool):
    connect_args = {"options": "-c default_transaction_read_only=on"} if read_only else {}
    return create_engine(url, connect_args=connect_args)


def current_alembic_revision(url: str) -> str | None:
    engine = _engine(url, read_only=True)
    try:
        with engine.connect() as conn:
            present = conn.scalar(text("SELECT to_regclass('public.alembic_version')"))
            if present is None:
                return None
            rows = conn.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    except SQLAlchemyError as exc:
        raise MaintenanceError(f"could not read alembic revision ({exc.__class__.__name__})") from None
    finally:
        engine.dispose()
    if len(rows) > 1:
        raise MaintenanceError("alembic_version has multiple rows")
    return rows[0] if rows else None


def postgres_version(url: str) -> str:
    engine = _engine(url, read_only=True)
    try:
        with engine.connect() as conn:
            version = conn.scalar(text("SHOW server_version"))
    except SQLAlchemyError as exc:
        raise MaintenanceError(f"could not read postgres version ({exc.__class__.__name__})") from None
    finally:
        engine.dispose()
    if not isinstance(version, str) or not version.strip():
        raise MaintenanceError("could not read postgres version")
    return version.strip()


def archive_object_rows(url: str) -> list[tuple[str, int, str]]:
    engine = _engine(url, read_only=True)
    try:
        with engine.connect() as conn:
            present = conn.scalar(text("SELECT to_regclass('public.archive_objects')"))
            if present is None:
                raise MaintenanceError("archive_objects is missing")
            result = conn.execute(
                text(
                    "SELECT sha256, size_bytes, relative_path "
                    "FROM archive_objects ORDER BY sha256, relative_path"
                )
            )
            rows = [(row.sha256, row.size_bytes, row.relative_path) for row in result]
    except SQLAlchemyError as exc:
        raise MaintenanceError(f"could not read archive objects ({exc.__class__.__name__})") from None
    finally:
        engine.dispose()
    return rows


def database_is_empty(url: str) -> None:
    engine = _engine(url, read_only=True)
    try:
        with engine.connect() as conn:
            count = conn.scalar(
                text(
                    "SELECT count(*) FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast') "
                    "AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')"
                )
            )
    except SQLAlchemyError as exc:
        raise MaintenanceError(f"could not inspect the target database ({exc.__class__.__name__})") from None
    finally:
        engine.dispose()
    if count:
        raise MaintenanceError("target database is not empty")


def _require_real_directory(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise MaintenanceError(f"{label} must be a real directory")
    return path


def iter_committed_files(archive_root: Path) -> list[tuple[Path, str]]:
    _require_real_directory(archive_root, "archive root")
    root = archive_root.resolve()
    found: list[tuple[Path, str]] = []
    for prefix in COMMITTED_PREFIXES:
        base = archive_root / prefix
        if not base.exists():
            continue
        if base.is_symlink() or not base.is_dir():
            raise MaintenanceError("symlink is not allowed in the archive")
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            current = Path(dirpath)
            if current.is_symlink():
                raise MaintenanceError("symlink is not allowed in the archive")
            for dirname in dirnames:
                child = current / dirname
                if child.is_symlink():
                    raise MaintenanceError("symlink is not allowed in the archive")
            for filename in filenames:
                path = current / filename
                if path.is_symlink():
                    raise MaintenanceError("symlink is not allowed in the archive")
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise MaintenanceError("archive entry is not a regular file")
                resolved = path.resolve()
                if root != resolved and root not in resolved.parents:
                    raise MaintenanceError("archive file escapes the archive root")
                relative = path.relative_to(archive_root).as_posix()
                if "staging" in Path(relative).parts:
                    raise MaintenanceError("staging is excluded from backups")
                found.append((path, relative))
    found.sort(key=lambda item: item[1])
    return found


def _hash_record(path: str, digest: object, size: object) -> dict[str, object]:
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise MaintenanceError("file size is invalid")
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise MaintenanceError("file hash is invalid")
    return {"path": path, "sha256": digest, "size_bytes": size}


def _file_record(path: Path, relative: str) -> dict[str, object]:
    size, digest = hash_regular_file(path)
    return _hash_record(relative, digest, size)


def verify_object_bytes(archive_root: Path, rows: list[tuple[str, int, str]]) -> None:
    seen: set[str] = set()
    for sha256, size_bytes, relative_path in rows:
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise MaintenanceError("archive object hash is invalid")
        if sha256 in seen:
            raise MaintenanceError("archive object digest is duplicated")
        seen.add(sha256)
        if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 0:
            raise MaintenanceError("archive object size is invalid")
        relative = committed_relative(relative_path)
        path = archive_root / relative
        if path.is_symlink() or not path.is_file():
            raise MaintenanceError("archive object file is missing")
        actual_size, digest = hash_regular_file(path)
        if digest != sha256 or actual_size != size_bytes:
            raise MaintenanceError("archive object bytes do not match the database")


def _script() -> ScriptDirectory:
    return ScriptDirectory(str(GATEWAY_DIR / "alembic"))


def can_move_forward(current: str | None, head: str) -> bool:
    """True when ``current`` is ``head`` or an ancestor of the single head."""
    script = _script()
    if current == head:
        return True
    seen: set[str] = set()
    node: str | None = head
    while node and node not in seen:
        if node == current:
            return True
        seen.add(node)
        revision = script.get_revision(node)
        if revision is None:
            return False
        down = revision.down_revision
        if isinstance(down, tuple):
            return False
        if down is None:
            return current is None
        node = down
    return False


def _publish_directory(temp: Path, dest: Path) -> None:
    os.chmod(temp, 0o700)
    _fsync_directory(temp)
    try:
        os.rename(temp, dest)
    except OSError:
        raise MaintenanceError("could not publish the backup directory") from None


def backup(database_url: str, expect_database: str, archive_root: Path, dest: Path) -> None:
    target = open_target(database_url, expect_database, operation="backup")
    archive_root = _require_real_directory(archive_root, "archive root")
    if dest.exists() or dest.is_symlink():
        raise MaintenanceError("backup destination already exists")
    if not dest.parent.is_dir() or dest.parent.is_symlink():
        raise MaintenanceError("backup destination parent must be a real directory")
    temp = dest.parent / f".{dest.name}.partial-{uuid.uuid4().hex}"
    temp.mkdir(mode=0o700)
    os.chmod(temp, 0o700)
    try:
        files = iter_committed_files(archive_root)
        dump_path = temp / DUMP_NAME
        _run(pg_dump_argv(dump_path), env=libpq_env(target), secrets=target.secrets)
        os.chmod(dump_path, 0o600)
        with dump_path.open("rb") as handle:
            os.fsync(handle.fileno())
        rows = archive_object_rows(target.url_text)
        verify_object_bytes(archive_root, rows)
        referenced = {committed_relative(relative) for _sha, _size, relative in rows}
        copied = {relative for _path, relative in files if relative.startswith("archive/objects/")}
        for relative in sorted(copied - referenced):
            print(f"orphan archive file: {relative}", file=sys.stderr)
        records = [_file_record(dump_path, DUMP_NAME)]
        for source, relative in files:
            _copy_regular_file(source, temp / relative, 0o600)
            records.append(_file_record(temp / relative, relative))
        records.sort(key=lambda item: str(item["path"]))
        by_path = {committed_relative(relative): (sha256, size) for sha256, size, relative in rows}
        for record in records:
            expected = by_path.get(record["path"])
            if expected is None:
                continue
            sha256, size = expected
            if record["sha256"] != sha256 or record["size_bytes"] != size:
                raise MaintenanceError("archive object bytes do not match the database")
        dump_record = next(item for item in records if item["path"] == DUMP_NAME)
        archive_records = [item for item in records if item["path"] != DUMP_NAME]
        inventory = {
            "format": FORMAT,
            "created_at": _utc_now(),
            "source_database": target.name,
            "alembic_revision": current_alembic_revision(target.url_text),
            "git_commit": git_commit(),
            "pg_dump_version": tool_version("pg_dump"),
            "postgres_version": postgres_version(target.url_text),
            "archive_file_count": len(archive_records),
            "archive_total_bytes": sum(int(item["size_bytes"]) for item in archive_records),
            "database_dump": dump_record,
            "files": records,
        }
        payload = canonical_json(inventory)
        inventory_path = temp / INVENTORY_NAME
        inventory_path.write_bytes(payload)
        os.chmod(inventory_path, 0o600)
        digest = sha256_bytes(payload)
        hash_path = temp / INVENTORY_HASH_NAME
        hash_path.write_text(f"{digest}  {INVENTORY_NAME}\n", encoding="utf-8")
        os.chmod(hash_path, 0o600)
        with inventory_path.open("rb") as handle:
            os.fsync(handle.fileno())
        with hash_path.open("rb") as handle:
            os.fsync(handle.fileno())
        _publish_directory(temp, dest)
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    print(f"backup complete: {dest.name}", file=sys.stderr)


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_inventory(source: Path) -> dict:
    source = _require_real_directory(source, "backup source")
    inventory_path = source / INVENTORY_NAME
    hash_path = source / INVENTORY_HASH_NAME
    if inventory_path.is_symlink() or hash_path.is_symlink():
        raise MaintenanceError("symlink is not allowed in the backup")
    if not inventory_path.is_file() or not hash_path.is_file():
        raise MaintenanceError("backup inventory is missing")
    payload = inventory_path.read_bytes()
    expected = hash_path.read_text(encoding="utf-8").strip().split()
    if len(expected) != 2 or expected[1] != INVENTORY_NAME or expected[0] != sha256_bytes(payload):
        raise MaintenanceError("backup inventory hash mismatch")
    try:
        inventory = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise MaintenanceError("backup inventory is invalid") from None
    if not isinstance(inventory, dict) or inventory.get("format") != FORMAT:
        raise MaintenanceError("backup inventory is invalid")
    files = inventory.get("files")
    database_dump = inventory.get("database_dump")
    if not isinstance(files, list) or not isinstance(database_dump, dict):
        raise MaintenanceError("backup inventory is invalid")
    seen: set[str] = set()
    listed: dict[str, dict] = {}
    for item in files:
        if not isinstance(item, dict):
            raise MaintenanceError("backup inventory is invalid")
        path = backup_member(item.get("path"))
        record = _hash_record(path, item.get("sha256"), item.get("size_bytes"))
        if path in seen:
            raise MaintenanceError("backup member path is duplicated")
        seen.add(path)
        listed[path] = record
        file_path = source / path
        if file_path.is_symlink() or not file_path.is_file():
            raise MaintenanceError("backup member is missing")
        size, digest = hash_regular_file(file_path)
        if digest != record["sha256"] or size != record["size_bytes"]:
            raise MaintenanceError("backup member bytes do not match the inventory")
    dump_path = backup_member(database_dump.get("path"))
    if dump_path != DUMP_NAME or listed.get(DUMP_NAME) != _hash_record(
        DUMP_NAME, database_dump.get("sha256"), database_dump.get("size_bytes")
    ):
        raise MaintenanceError("database dump record does not match the inventory")
    present = _regular_files(source)
    allowed = set(listed) | {INVENTORY_NAME, INVENTORY_HASH_NAME}
    if present != allowed:
        raise MaintenanceError("backup directory does not match the inventory")
    return inventory


def _regular_files(root: Path) -> set[str]:
    found: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        if current != root and current.is_symlink():
            raise MaintenanceError("symlink is not allowed in the backup")
        for dirname in dirnames:
            if (current / dirname).is_symlink():
                raise MaintenanceError("symlink is not allowed in the backup")
        for filename in filenames:
            path = current / filename
            if path.is_symlink():
                raise MaintenanceError("symlink is not allowed in the backup")
            found.add(path.relative_to(root).as_posix())
    return found


def verify(source: Path) -> None:
    inventory = load_inventory(source)
    _run(pg_restore_argv(source / DUMP_NAME, listing=True), env={"PATH": os.environ.get("PATH", "")}, secrets=())
    print(
        f"backup verified: database {inventory.get('source_database')} revision {inventory.get('alembic_revision')}",
        file=sys.stderr,
    )


def _read_regular_bytes(path: Path) -> bytes:
    fd = _open_nofollow(path)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise MaintenanceError("manifest is not a regular file")
        chunks: list[bytes] = []
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


def archive_object_detail_rows(url: str) -> list[dict[str, object]]:
    engine = _engine(url, read_only=True)
    try:
        with engine.connect() as conn:
            present = conn.scalar(text("SELECT to_regclass('public.archive_objects')"))
            if present is None:
                raise MaintenanceError("archive_objects is missing")
            result = conn.execute(
                text(
                    "SELECT id::text AS id, sha256, size_bytes, relative_path, media_type, "
                    "integrity_status, integrity_error, integrity_checked_at "
                    "FROM archive_objects ORDER BY sha256, relative_path"
                )
            )
            rows = [dict(row._mapping) for row in result]
    except SQLAlchemyError as exc:
        raise MaintenanceError(f"could not read archive objects ({exc.__class__.__name__})") from None
    finally:
        engine.dispose()
    return rows


def _table_exists(conn, table: str) -> bool:
    if table not in {name for name, _nullable in EVIDENCE_LINK_TABLES}:
        raise MaintenanceError("evidence table is missing")
    present = conn.scalar(text("SELECT to_regclass(:qualified)"), {"qualified": f"public.{table}"})
    return present is not None


def _broken_observation_links(url: str) -> None:
    engine = _engine(url, read_only=True)
    try:
        with engine.connect() as conn:
            for table, nullable in EVIDENCE_LINK_TABLES:
                if not _table_exists(conn, table):
                    raise MaintenanceError(f"evidence table is missing: {table}")
                null_guard = f"{table}.archive_object_id IS NOT NULL AND " if nullable else ""
                count = conn.scalar(
                    text(
                        f"SELECT count(*) FROM {table} "
                        f"LEFT JOIN archive_objects ON archive_objects.id = {table}.archive_object_id "
                        f"WHERE {null_guard}archive_objects.id IS NULL"
                    )
                )
                if count:
                    raise MaintenanceError(f"{table} rows are not linked to an archive object")
    except SQLAlchemyError as exc:
        raise MaintenanceError(f"could not read evidence links ({exc.__class__.__name__})") from None
    finally:
        engine.dispose()


def _timestamps_match(stored: object, rendered: object) -> bool:
    if stored is None or rendered is None:
        return stored is None and rendered is None
    if not isinstance(stored, datetime) or not isinstance(rendered, str) or not rendered:
        return False
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError:
        return False
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return stored.astimezone(UTC) == parsed.astimezone(UTC)


def _manifest_problem(document: object, row: dict[str, object]) -> str | None:
    if not isinstance(document, dict) or any(key not in document for key in MANIFEST_REQUIRED_KEYS):
        return "archive manifest is incomplete"
    version = document.get("manifest_version")
    if isinstance(version, bool) or version != 2:
        return "archive manifest is incomplete"
    size = document.get("size_bytes")
    if isinstance(size, bool) or not isinstance(size, int):
        return "archive manifest is incomplete"
    identity_matches = (
        document.get("object_id") == row["id"]
        and document.get("sha256") == row["sha256"]
        and document.get("expected_sha256") == row["sha256"]
        and size == row["size_bytes"]
        and document.get("relative_path") == row["relative_path"]
        and document.get("media_type") == row["media_type"]
        and document.get("integrity_status") == row["integrity_status"]
        and document.get("integrity_error") == row["integrity_error"]
        and _timestamps_match(row["integrity_checked_at"], document.get("integrity_checked_at"))
    )
    if not identity_matches:
        return "archive manifest does not match the archive object"
    return None


def _verify_manifest_file(archive_root: Path, row: dict[str, object]) -> None:
    sha256 = row["sha256"]
    if not isinstance(sha256, str) or len(sha256) != 64:
        raise MaintenanceError("archive object hash is invalid")
    path = archive_root / "manifests" / f"{sha256}.json"
    if path.is_symlink() or not path.exists():
        raise MaintenanceError("archive manifest is missing")
    try:
        payload = _read_regular_bytes(path)
    except MaintenanceError:
        raise
    except OSError:
        raise MaintenanceError("archive manifest is missing") from None
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise MaintenanceError("archive manifest is incomplete") from None
    problem = _manifest_problem(document, row)
    if problem is not None:
        raise MaintenanceError(problem)


def verify_evidence(database_url: str, expect_database: str, archive_root: Path) -> None:
    """Check live observation links, object bytes, and manifest completeness."""
    target = open_target(database_url, expect_database, operation="verify")
    archive_root = _require_real_directory(archive_root, "archive root")
    _broken_observation_links(target.url_text)
    rows = archive_object_detail_rows(target.url_text)
    verify_object_bytes(
        archive_root,
        [
            (str(row["sha256"]), int(row["size_bytes"]), str(row["relative_path"]))
            for row in rows
        ],
    )
    for row in rows:
        _verify_manifest_file(archive_root, row)
    print(f"evidence verified: {len(rows)} archive objects", file=sys.stderr)


def repair_manifests(database_url: str, expect_database: str, archive_root: Path) -> None:
    """Write missing manifests from committed archive rows. Never replace an existing file."""
    target = open_target(database_url, expect_database, operation="repair")
    archive_root = _require_real_directory(archive_root, "archive root")
    if not os.environ.get("DATABASE_URL"):
        os.environ["DATABASE_URL"] = target.url_text
    from app.archive.manifest import ManifestRepairRefused, repair_missing_manifests
    from app.models import ArchiveObject
    from sqlalchemy.orm import Session

    engine = _engine(target.url_text, read_only=True)
    try:
        try:
            with Session(engine) as session:
                records = list(
                    session.scalars(
                        select(ArchiveObject).order_by(ArchiveObject.sha256, ArchiveObject.id)
                    )
                )
                try:
                    written, kept = repair_missing_manifests(archive_root, records)
                except ManifestRepairRefused as exc:
                    raise MaintenanceError(str(exc)) from None
        except SQLAlchemyError as exc:
            raise MaintenanceError(f"could not read archive objects ({exc.__class__.__name__})") from None
    finally:
        engine.dispose()
    print(f"repair-manifests complete: wrote {written}, kept {kept}", file=sys.stderr)


def restore(database_url: str, expect_database: str, archive_root: Path, source: Path) -> None:
    # Protected names are rejected before a connection or a file copy.
    if expect_database.lower() in PROTECTED_RESTORE_DATABASES:
        raise MaintenanceError(f"refusing restore into protected database {expect_database}")
    inventory = load_inventory(source)
    source_database = inventory.get("source_database")
    if not isinstance(source_database, str) or not source_database:
        raise MaintenanceError("backup inventory is invalid")
    if source_database == expect_database:
        raise MaintenanceError("restore target matches the backup source database")
    archive_root = _require_real_directory(archive_root, "archive root")
    if any(archive_root.iterdir()):
        raise MaintenanceError("archive root must be an empty directory")
    target = open_target(database_url, expect_database, operation="restore")
    database_is_empty(target.url_text)
    _run(pg_restore_argv(source / DUMP_NAME, listing=True), env=libpq_env(target), secrets=target.secrets)
    _run(
        pg_restore_argv(source / DUMP_NAME, listing=False, database=target.name),
        env=libpq_env(target),
        secrets=target.secrets,
    )
    for relative, record in sorted(
        (
            (item["path"], item)
            for item in inventory["files"]
            if item["path"] != DUMP_NAME
        ),
        key=lambda item: item[0],
    ):
        _copy_regular_file(source / relative, archive_root / relative, 0o644)
        size, digest = hash_regular_file(archive_root / relative)
        if digest != record["sha256"] or size != record["size_bytes"]:
            raise MaintenanceError("restored archive file does not match the inventory")
    if (archive_root / "staging").exists():
        raise MaintenanceError("staging is excluded from backups")
    revision = current_alembic_revision(target.url_text)
    if revision != inventory.get("alembic_revision"):
        raise MaintenanceError("alembic revision does not match the backup")
    verify_object_bytes(archive_root, archive_object_rows(target.url_text))
    print(f"restore complete: {target.name}", file=sys.stderr)


def upgrade(
    database_url: str,
    expect_database: str,
    *,
    archive_root: Path | None,
    backup_dir: Path | None,
) -> None:
    target = open_target(database_url, expect_database, operation="upgrade")
    if backup_dir is not None:
        if archive_root is None:
            raise MaintenanceError("upgrade --backup-dir requires --archive-root")
        backup(target.url_text, target.name, archive_root, backup_dir)
    script = _script()
    heads = script.get_heads()
    if len(heads) != 1:
        raise MaintenanceError("alembic history has multiple heads")
    head = heads[0]
    current = current_alembic_revision(target.url_text)
    if not can_move_forward(current, head):
        raise MaintenanceError("upgrade refused because the database revision is not an ancestor of head")
    if current == head:
        print(f"already at {head}", file=sys.stderr)
        return
    env = os.environ.copy()
    env["DATABASE_URL"] = target.url_text
    result = _run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=env,
        secrets=target.secrets,
        cwd=GATEWAY_DIR,
    )
    scrubbed = scrub(f"{result.stdout}\n{result.stderr}", target.secrets).strip()
    if scrubbed:
        print(scrubbed, file=sys.stderr)
    after = current_alembic_revision(target.url_text)
    if after != head:
        raise MaintenanceError("upgrade finished at an unexpected revision")
    print(f"upgrade complete: {current or 'base'} -> {head}", file=sys.stderr)


def _path(value: str) -> Path:
    return Path(value)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.maintenance",
        description=(
            "Back up or restore one Gateway database and its committed archive, "
            "or apply forward-only Alembic upgrades. "
            "Prefer DATABASE_URL over --database-url so the password is not in the process list. "
            "Downgrade is not supported."
        ),
    )
    parser.add_argument("--database-url", default=None, help="Defaults to DATABASE_URL")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser("backup", help="Write a new backup directory")
    backup_parser.add_argument("--expect-database", required=True)
    backup_parser.add_argument("--archive-root", required=True, type=_path)
    backup_parser.add_argument("--dest", required=True, type=_path)

    restore_parser = subparsers.add_parser("restore", help="Restore into a new empty database and archive root")
    restore_parser.add_argument("--expect-database", required=True)
    restore_parser.add_argument("--archive-root", required=True, type=_path)
    restore_parser.add_argument("--source", required=True, type=_path)

    upgrade_parser = subparsers.add_parser("upgrade", help="Apply Alembic upgrades through head")
    upgrade_parser.add_argument("--expect-database", required=True)
    upgrade_parser.add_argument("--archive-root", type=_path)
    upgrade_parser.add_argument("--backup-dir", type=_path)

    repair_parser = subparsers.add_parser(
        "repair-manifests",
        help="Write manifest files that are missing; do not overwrite existing ones",
    )
    repair_parser.add_argument("--expect-database", required=True)
    repair_parser.add_argument("--archive-root", required=True, type=_path)

    verify_parser = subparsers.add_parser(
        "verify",
        help="Check a backup directory, or check live evidence links and manifests",
    )
    verify_parser.add_argument("--source", type=_path, help="Backup directory to check")
    verify_parser.add_argument(
        "--evidence",
        action="store_true",
        help="Check observation links, object bytes, and manifest completeness",
    )
    verify_parser.add_argument("--expect-database")
    verify_parser.add_argument("--archive-root", type=_path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] == "downgrade":
        print(
            "error: downgrade is not supported; forward upgrade is the only migration operation",
            file=sys.stderr,
        )
        return 2
    try:
        args = parse_args(args_list)
        if args.command == "verify" and not args.evidence:
            if args.source is None:
                raise MaintenanceError("verify requires --source or --evidence")
            verify(args.source)
            return 0
        database_url = require_database_url(args.database_url)
        if args.command == "verify":
            if args.source is not None:
                raise MaintenanceError("verify --evidence does not take --source")
            if not args.expect_database or args.archive_root is None:
                raise MaintenanceError(
                    "verify --evidence requires --expect-database and --archive-root"
                )
            verify_evidence(database_url, args.expect_database, args.archive_root)
        elif args.command == "repair-manifests":
            repair_manifests(database_url, args.expect_database, args.archive_root)
        elif args.command == "backup":
            backup(database_url, args.expect_database, args.archive_root, args.dest)
        elif args.command == "restore":
            restore(database_url, args.expect_database, args.archive_root, args.source)
        elif args.command == "upgrade":
            upgrade(
                database_url,
                args.expect_database,
                archive_root=args.archive_root,
                backup_dir=args.backup_dir,
            )
        else:
            raise MaintenanceError("unknown maintenance command")
    except MaintenanceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc.__class__.__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
