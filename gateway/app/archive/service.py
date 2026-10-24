import hashlib
import logging
import mimetypes
import os
import re
import shutil
import stat
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..importers.staging import UnsafeStagingRoot, create_prejob_generation
from ..models import ArchiveObject, ArchiveObservation
from .manifest import sync_manifest_from_archive_object

SAFE_NAME = re.compile(r"^[^<>:\"/\\|?*\x00-\x1f]+$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
logger = logging.getLogger(__name__)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class PreparedObject:
    """Verified bytes published to the content-addressed object store."""

    sha256: str
    size_bytes: int
    relative_path: str
    duplicate: bool
    media_type: str
    original_filename: str
    source_platform: str
    # Runtime filesystem paths are process-local implementation details, not
    # durable source provenance. Source-native locators belong in logical_path
    # and observation_metadata instead.
    source_path: str | None
    logical_path: str
    observation_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PendingArchiveResult:
    """Archive rows flushed, but intentionally not committed, by this service."""

    prepared: PreparedObject
    object_id: uuid.UUID
    observation_id: uuid.UUID
    record: ArchiveObject
    observation: ArchiveObservation

    @property
    def archive_object_id(self) -> uuid.UUID:
        return self.object_id


@dataclass(frozen=True)
class ArchiveResult:
    object_id: uuid.UUID
    sha256: str
    size_bytes: int
    relative_path: str
    duplicate: bool
    manifest_path: Path
    observation_id: uuid.UUID


class ArchiveIntegrityError(IOError):
    def __init__(self, message: str, *, sha256: str | None = None, actual_sha256: str | None = None):
        super().__init__(message)
        self.sha256 = sha256
        self.actual_sha256 = actual_sha256


class ArchiveService:
    def __init__(self, root: Path, db: Session, fault_hook=None):
        self.root = root
        self.staging = root / "staging"
        self.objects = root / "archive" / "objects"
        self.manifests = root / "manifests"
        self.db = db
        self.fault_hook = fault_hook or (lambda event: None)

    @staticmethod
    def _is_link_or_reparse(path: Path) -> bool:
        details = path.lstat()
        attributes = getattr(details, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return stat.S_ISLNK(details.st_mode) or bool(attributes & reparse_flag)

    def _ensure_safe_directory(self, *parts: str) -> Path:
        """Create trusted-root children without traversing visible reparse points.

        The configured archive root is a permission boundary and must not be
        writable by untrusted principals. Python lacks the Windows handle-relative
        primitives needed to eliminate every check/use race, so the root ACL is the
        remaining TOCTOU control after these component checks.
        """
        # Creating the configured root is allowed by the trusted-root invariant;
        # every service-owned child is then created and checked one component at a time.
        self.root.mkdir(parents=True, exist_ok=True)
        root_resolved = self.root.resolve(strict=True)
        if self._is_link_or_reparse(self.root):
            raise ArchiveIntegrityError("archive_integrity_failure: unsafe archive root")
        current = self.root
        for part in parts:
            current = current / part
            try:
                current.mkdir()
            except FileExistsError:
                pass
            if not current.is_dir() or self._is_link_or_reparse(current):
                raise ArchiveIntegrityError("archive_integrity_failure: unsafe archive path")
            try:
                current.resolve(strict=True).relative_to(root_resolved)
            except (FileNotFoundError, OSError, ValueError) as exc:
                raise ArchiveIntegrityError("archive_integrity_failure: unsafe archive path") from exc
        return current

    def _validated_target(self, relative_path: str, *, must_exist: bool) -> Path:
        expected_prefix = Path("archive") / "objects"
        relative = Path(relative_path)
        if relative.is_absolute() or relative.parts[:2] != expected_prefix.parts:
            raise ArchiveIntegrityError("archive_integrity_failure: unsafe object path")
        target = self.root / relative
        try:
            target.relative_to(self.objects)
        except ValueError as exc:
            raise ArchiveIntegrityError("archive_integrity_failure: unsafe object path") from exc

        # Reject link-like ancestors lexically before resolving so a junction cannot
        # redirect publication outside the configured content-addressed root.
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.exists() and self._is_link_or_reparse(current):
                raise ArchiveIntegrityError("archive_integrity_failure: unsafe object path")
        try:
            target.resolve(strict=must_exist).relative_to(self.objects.resolve(strict=True))
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise ArchiveIntegrityError("archive_integrity_failure: unsafe object path") from exc
        return target

    def _ensure_object_parent(self, digest_prefix: str) -> None:
        """Create archive directories without traversing a link or reparse point."""
        self._ensure_safe_directory("archive", "objects", digest_prefix)

    def prepare(
        self,
        source: Path,
        original_filename: str | None = None,
        source_platform: str = "unknown",
        media_type: str | None = None,
        *,
        logical_path: str | None = None,
        observation_metadata: dict[str, Any] | None = None,
    ) -> PreparedObject:
        """Publish and verify bytes without reading or mutating the database."""
        filename = original_filename or source.name
        if not SAFE_NAME.fullmatch(filename) or Path(filename).name != filename:
            raise ValueError("非法文件名")
        if not source.is_file():
            raise FileNotFoundError(source)

        try:
            generation = create_prejob_generation(self.root, "archive-prepared")
        except UnsafeStagingRoot as exc:
            raise ArchiveIntegrityError(
                "archive_integrity_failure: unsafe archive path"
            ) from exc
        staged = generation.payload_path()
        try:
            shutil.copyfile(source, staged)
            size = staged.stat().st_size
            sha = _sha256_file(staged)
            rel = Path("archive") / "objects" / sha[:2] / sha
            target = self.root / rel
            self._ensure_object_parent(sha[:2])
            target = self._validated_target(rel.as_posix(), must_exist=False)
            with staged.open("r+b") as staged_file:
                os.fsync(staged_file.fileno())
            try:
                os.link(staged, target)
                duplicate = False
            except FileExistsError:
                duplicate = True

            target = self._validated_target(rel.as_posix(), must_exist=True)
            if not target.is_file() or target.stat().st_size != size:
                actual = _sha256_file(target) if target.is_file() else None
                raise ArchiveIntegrityError(
                    "archive_integrity_failure: object size mismatch",
                    sha256=sha,
                    actual_sha256=actual,
                )
            actual = _sha256_file(target)
            if actual != sha:
                message = (
                    "archive_integrity_failure: existing object digest mismatch"
                    if duplicate
                    else "archive_integrity_failure: post-write digest mismatch"
                )
                raise ArchiveIntegrityError(message, sha256=sha, actual_sha256=actual)

            self.fault_hook("file_published")
            observed_media_type = media_type or mimetypes.guess_type(source.name)[0] or "application/octet-stream"
            metadata = dict(observation_metadata or {})
            metadata.setdefault("observed_media_type", observed_media_type)
            return PreparedObject(
                sha256=sha,
                size_bytes=size,
                relative_path=rel.as_posix(),
                duplicate=duplicate,
                media_type=observed_media_type,
                original_filename=filename,
                source_platform=source_platform,
                source_path=None,
                logical_path=logical_path or filename,
                observation_metadata=metadata,
            )
        finally:
            cleanup = generation.close(remove=True)
            if cleanup is not None and cleanup.status not in {"deleted", "missing"}:
                logger.warning(
                    "archive prepare staging cleanup incomplete",
                    extra={"cleanup_outcome": cleanup.status},
                )

    prepare_file = prepare

    def persist_prepared(
        self,
        prepared: PreparedObject,
        *,
        import_job_id: uuid.UUID | None = None,
    ) -> PendingArchiveResult:
        """Flush the object and observation into the caller-owned transaction."""
        if not SHA256_HEX.fullmatch(prepared.sha256):
            raise ArchiveIntegrityError("archive_integrity_failure: invalid object digest")
        expected_relative_path = (Path("archive") / "objects" / prepared.sha256[:2] / prepared.sha256).as_posix()
        if prepared.relative_path != expected_relative_path:
            raise ArchiveIntegrityError("archive_integrity_failure: object path mismatch", sha256=prepared.sha256)
        self.db.execute(select(func.pg_advisory_xact_lock(func.hashtext(prepared.sha256))))
        target = self._validated_target(prepared.relative_path, must_exist=True)
        if not target.is_file() or target.stat().st_size != prepared.size_bytes:
            raise ArchiveIntegrityError("archive_integrity_failure: object size mismatch", sha256=prepared.sha256)
        actual = _sha256_file(target)
        if actual != prepared.sha256:
            raise ArchiveIntegrityError(
                "archive_integrity_failure: object digest mismatch",
                sha256=prepared.sha256,
                actual_sha256=actual,
            )

        records = list(
            self.db.scalars(
                select(ArchiveObject)
                .where(ArchiveObject.sha256 == prepared.sha256)
                .execution_options(populate_existing=True)
            )
        )
        if len(records) > 1:
            raise ArchiveIntegrityError("archive_integrity_failure: ambiguous digest record", sha256=prepared.sha256)
        record = records[0] if records else None
        checked_at = datetime.now(UTC)
        if record:
            if record.size_bytes != prepared.size_bytes or record.relative_path != prepared.relative_path:
                raise ArchiveIntegrityError("archive_integrity_failure: object record mismatch", sha256=prepared.sha256)
            if record.integrity_status != "verified":
                raise ArchiveIntegrityError(
                    "archive_integrity_failure: explicit reverify required",
                    sha256=prepared.sha256,
                )
            record.integrity_checked_at = checked_at
        else:
            record = ArchiveObject(
                sha256=prepared.sha256,
                size_bytes=prepared.size_bytes,
                relative_path=prepared.relative_path,
                media_type=prepared.media_type,
                integrity_status="verified",
                integrity_checked_at=checked_at,
                metadata_json={},
            )
            self.db.add(record)
            self.db.flush()

        observation = ArchiveObservation(
            archive_object_id=record.id,
            original_filename=prepared.original_filename,
            source_platform=prepared.source_platform,
            source_path=prepared.source_path,
            logical_path=prepared.logical_path,
            import_job_id=import_job_id,
            metadata_json=dict(prepared.observation_metadata),
        )
        self.db.add(observation)
        self.db.flush()
        return PendingArchiveResult(
            prepared=prepared,
            object_id=record.id,
            observation_id=observation.id,
            record=record,
            observation=observation,
        )

    def finalize(self, pending: PendingArchiveResult) -> ArchiveResult:
        """Write a manifest from a freshly locked, committed database row."""
        prepared = pending.prepared
        bind = self.db.get_bind()
        with Session(bind) as verifier:
            verifier.execute(select(func.pg_advisory_xact_lock(func.hashtext(prepared.sha256))))
            committed = verifier.scalar(
                select(ArchiveObject).where(
                    ArchiveObject.id == pending.object_id,
                    ArchiveObject.sha256 == prepared.sha256,
                    ArchiveObject.size_bytes == prepared.size_bytes,
                ).execution_options(populate_existing=True)
            )
            if committed is None or committed.relative_path != prepared.relative_path:
                raise RuntimeError("archive object must be committed before finalization")
            committed_observation = verifier.scalar(
                select(ArchiveObservation).where(
                    ArchiveObservation.id == pending.observation_id,
                    ArchiveObservation.archive_object_id == pending.object_id,
                )
            )
            if committed_observation is None:
                raise RuntimeError("archive observation must be committed before finalization")
            manifest_path = self._write_manifest(committed)
        return ArchiveResult(
            object_id=pending.object_id,
            sha256=prepared.sha256,
            size_bytes=prepared.size_bytes,
            relative_path=prepared.relative_path,
            duplicate=prepared.duplicate,
            manifest_path=manifest_path,
            observation_id=pending.observation_id,
        )

    def archive_file(
        self,
        source: Path,
        original_filename: str | None = None,
        source_platform: str = "unknown",
        media_type: str | None = None,
    ) -> ArchiveResult:
        """Backward-compatible single-call archive workflow."""
        try:
            prepared = self.prepare(source, original_filename, source_platform, media_type)
        except ArchiveIntegrityError as exc:
            self._record_integrity_failure(exc)
            raise

        try:
            pending = self.persist_prepared(prepared)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.fault_hook("database_committed")
        return self.finalize(pending)

    def cleanup_staging(self):
        from ..importers.cleanup import cleanup_failed_staging

        return cleanup_failed_staging(self.root, self.db)

    def _record_integrity_failure(self, error: ArchiveIntegrityError) -> None:
        if not error.sha256 or not error.actual_sha256:
            return
        bind = self.db.get_bind()
        with bind.connect() as connection:
            connection.execute(select(func.pg_advisory_lock(func.hashtext(error.sha256))))
            connection.commit()
            try:
                with Session(connection) as marker:
                    records = list(
                        marker.scalars(
                            select(ArchiveObject)
                            .where(ArchiveObject.sha256 == error.sha256)
                            .execution_options(populate_existing=True)
                        )
                    )
                    if len(records) != 1:
                        marker.rollback()
                        return
                    record = records[0]
                    record.integrity_status = "integrity_failure"
                    record.integrity_error = f"digest_mismatch:{error.actual_sha256}"
                    record.integrity_checked_at = datetime.now(UTC)
                    record_id = record.id
                    marker.commit()
                    committed = marker.scalar(
                        select(ArchiveObject)
                        .where(ArchiveObject.id == record_id)
                        .execution_options(populate_existing=True)
                    )
                    if committed is None:
                        raise RuntimeError("integrity marker commit was not visible")
                    self._write_manifest(committed)
            finally:
                connection.execute(select(func.pg_advisory_unlock(func.hashtext(error.sha256))))
                connection.commit()

    def _write_manifest(self, record: ArchiveObject) -> Path:
        manifests = self._ensure_safe_directory("manifests")
        target = manifests / f"{record.sha256}.json"
        if target.exists() and self._is_link_or_reparse(target):
            raise ArchiveIntegrityError("archive_integrity_failure: unsafe manifest path")
        written = sync_manifest_from_archive_object(self.root, record)
        if written != target or self._is_link_or_reparse(written):
            raise ArchiveIntegrityError("archive_integrity_failure: unsafe manifest path")
        return written
