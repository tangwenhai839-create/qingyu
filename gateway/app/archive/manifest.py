import json
import os
import stat
import tempfile
from pathlib import Path

from ..models import ArchiveObject


def render_manifest(record: ArchiveObject) -> dict:
    return {
        "manifest_version": 2,
        "object_id": str(record.id),
        "sha256": record.sha256,
        "expected_sha256": record.sha256,
        "size_bytes": record.size_bytes,
        "relative_path": record.relative_path,
        "media_type": record.media_type,
        "integrity_status": record.integrity_status,
        "integrity_checked_at": record.integrity_checked_at.isoformat() if record.integrity_checked_at else None,
        "integrity_error": record.integrity_error,
    }


class ManifestRepairRefused(RuntimeError):
    """A manifest path that repair must not replace."""


def manifest_path(root: Path, sha256: str) -> Path:
    if not _sha256_filename(sha256):
        raise ManifestRepairRefused("archive object hash is invalid")
    return root / "manifests" / f"{sha256}.json"


def manifest_is_finalized(root: Path, sha256: str) -> bool:
    """True when a regular ``manifests/<sha256>.json`` file is already published.

    A missing file means finalize has not finished (or the file was removed).
    Symlinks and other non-regular entries are not treated as finalized.
    """
    if not _sha256_filename(sha256):
        return False
    path = root / "manifests" / f"{sha256}.json"
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode)


def repair_missing_manifests(root: Path, records) -> tuple[int, int]:
    """Write manifests that are absent. Existing regular files are left unchanged.

    Returns ``(written, kept)``. A symlink or other non-regular manifest path
    is refused instead of being replaced.
    """
    manifests = root / "manifests"
    if manifests.is_symlink():
        raise ManifestRepairRefused("refusing to overwrite an unsafe manifest path")
    written = 0
    kept = 0
    for record in records:
        path = manifest_path(root, record.sha256)
        try:
            info = path.lstat()
        except FileNotFoundError:
            sync_manifest_from_archive_object(root, record)
            written += 1
            continue
        except OSError as exc:
            raise ManifestRepairRefused("manifest path could not be inspected") from exc
        if not stat.S_ISREG(info.st_mode):
            raise ManifestRepairRefused("refusing to overwrite an unsafe manifest path")
        kept += 1
    return written, kept


def _sha256_filename(sha256: object) -> bool:
    return (
        isinstance(sha256, str)
        and len(sha256) == 64
        and all(char in "0123456789abcdef" for char in sha256)
    )


def sync_manifest_from_archive_object(root: Path, record: ArchiveObject) -> Path:
    manifests = root / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    target = manifests / f"{record.sha256}.json"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{record.sha256}-", suffix=".tmp", dir=manifests)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(render_manifest(record), output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
