"""Owned staging namespaces and safe lifecycle integration points.

The archive itself is deliberately outside this module's authority.  Every path
returned here is below ``<archive root>/staging``.
"""

from __future__ import annotations

import os
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, Self

PreJobNamespace = Literal["uploads", "archive-prepared"]
NAMESPACES = ("uploads", "archive-prepared", "jobs", "quarantine")


class UnsafeStagingRoot(RuntimeError):
    pass


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _link_like(path: Path) -> bool:
    details = os.lstat(path)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return (
        stat.S_ISLNK(details.st_mode)
        or bool(getattr(details, "st_file_attributes", 0) & reparse)
        or os.path.ismount(path)
    )


def _real_directory(path: Path, label: str) -> None:
    try:
        details = os.lstat(path)
    except OSError as exc:
        raise UnsafeStagingRoot(f"{label}_unavailable") from exc
    if not stat.S_ISDIR(details.st_mode) or _link_like(path):
        raise UnsafeStagingRoot(f"{label}_is_unsafe")


@dataclass(frozen=True)
class StagingLayout:
    root: Path
    staging: Path
    uploads: Path
    archive_prepared: Path
    jobs: Path
    quarantine: Path


def ensure_staging_layout(root: Path) -> StagingLayout:
    """Create fixed namespaces, failing closed on link/reparse components."""
    archive_root = _absolute(root)
    if not os.path.lexists(archive_root):
        archive_root.mkdir(parents=True, exist_ok=True)
    _real_directory(archive_root, "archive_root")
    staging = archive_root / "staging"
    if not os.path.lexists(staging):
        staging.mkdir(exist_ok=True)
    _real_directory(staging, "staging_root")
    values = []
    for name in NAMESPACES:
        candidate = staging / name
        if not os.path.lexists(candidate):
            candidate.mkdir(exist_ok=True)
        _real_directory(candidate, f"staging_namespace:{name}")
        values.append(candidate)
    return StagingLayout(archive_root, staging, *values)


def _lock(handle: BinaryIO, *, blocking: bool) -> bool:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        try:
            msvcrt.locking(handle.fileno(), mode, 1)
            return True
        except OSError:
            return False
    import fcntl
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(handle.fileno(), flags)
        return True
    except OSError:
        return False


def _unlock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _same_file(handle: BinaryIO, path: Path) -> bool:
    try:
        return os.path.samestat(os.fstat(handle.fileno()), os.stat(path, follow_symlinks=False))
    except OSError:
        return False


def _open_lock_file(path: Path) -> BinaryIO:
    if os.name != "nt":
        try:
            return path.open("x+b", buffering=0)
        except FileExistsError:
            return path.open("r+b", buffering=0)

    import ctypes
    import msvcrt
    from ctypes import wintypes

    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    native = create_file(
        str(path),
        0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
        0x1 | 0x2 | 0x4,  # share read, write and delete (rename while locked)
        None,
        4,  # OPEN_ALWAYS
        0x80 | 0x00200000,  # NORMAL | OPEN_REPARSE_POINT
        None,
    )
    invalid = wintypes.HANDLE(-1).value
    if native in (None, 0, invalid):
        raise OSError(ctypes.get_last_error(), "open_lock_file_failed")
    try:
        descriptor = msvcrt.open_osfhandle(native, os.O_RDWR | os.O_BINARY)
    except Exception:
        ctypes.windll.kernel32.CloseHandle(native)
        raise
    return os.fdopen(descriptor, "r+b", buffering=0)


@contextmanager
def held_lock(path: Path, *, blocking: bool = True):
    """Hold a one-byte OS lock and verify its published path did not change."""
    if os.path.lexists(path) and _link_like(path):
        raise UnsafeStagingRoot("lock_path_is_link_like")
    handle = _open_lock_file(path)
    if _link_like(path) or not _same_file(handle, path):
        handle.close()
        raise UnsafeStagingRoot("lock_path_changed")
    if os.fstat(handle.fileno()).st_size == 0:
        handle.write(b"0")
        handle.flush()
    if not _lock(handle, blocking=blocking):
        handle.close()
        yield None
        return
    try:
        if _link_like(path) or not _same_file(handle, path):
            raise UnsafeStagingRoot("lock_path_changed")
        yield handle
    finally:
        _unlock(handle)
        handle.close()


@dataclass
class PreJobGeneration:
    namespace: PreJobNamespace
    directory: Path
    _owner: BinaryIO
    _closed: bool = False

    def payload_path(self, filename: str = "payload") -> Path:
        if not filename or filename in {".", ".."} or Path(filename).name != filename:
            raise ValueError("payload filename must be a single path component")
        return self.directory / filename

    def close(self, *, remove: bool = False) -> SafeRemoveResult | None:
        if self._closed:
            return
        release_failed = False
        try:
            _unlock(self._owner)
        except OSError:
            release_failed = True
        finally:
            try:
                self._owner.close()
            except OSError:
                release_failed = True
            self._closed = True
        if release_failed:
            return SafeRemoveResult("failed", self.directory)
        if remove and self.directory.exists():
            try:
                return safe_remove_staging(self.directory, self.directory.parents[2])
            except Exception:  # noqa: BLE001
                # Cleanup is best-effort after the lifecycle owner has already
                # completed or failed. The periodic scanner will retry it.
                return SafeRemoveResult("failed", self.directory)
        return None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()


def create_prejob_generation(root: Path, namespace: PreJobNamespace) -> PreJobGeneration:
    if namespace not in ("uploads", "archive-prepared"):
        raise ValueError("unsupported pre-job staging namespace")
    layout = ensure_staging_layout(root)
    parent = layout.uploads if namespace == "uploads" else layout.archive_prepared
    with held_lock(parent / ".namespace.lock") as namespace_owner:
        if namespace_owner is None:
            raise OSError("unable to acquire staging namespace lock")
        directory = parent / str(uuid.uuid4())
        # Durable cleanup intent precedes visibility. A crash before mkdir
        # leaves only a harmless missing queue record; a crash after mkdir is
        # always discoverable.
        enqueue_staging_candidate(parent, directory.name)
        directory.mkdir()
        owner_path = directory / ".owner.lock"
        owner_path.write_bytes(b"0")
        owner = owner_path.open("r+b", buffering=0)
        if not _lock(owner, blocking=True):
            owner.close()
            raise OSError("unable to acquire staging ownership lock")
        if (
            _link_like(directory)
            or _link_like(owner_path)
            or not _same_file(owner, owner_path)
            or not directory.exists()
        ):
            _unlock(owner)
            owner.close()
            raise UnsafeStagingRoot("generation_publish_changed")
    return PreJobGeneration(namespace, directory, owner)


def generation_is_active(directory: Path) -> bool | None:
    """Return True for a held OS lock, False for abandoned, None if malformed."""
    owner_path = directory / ".owner.lock"
    if not owner_path.is_file() or _link_like(owner_path):
        return None
    try:
        owner = owner_path.open("r+b", buffering=0)
    except OSError:
        return True
    try:
        acquired = _lock(owner, blocking=False)
        if acquired:
            _unlock(owner)
        return not acquired
    finally:
        owner.close()


def job_attempt_path(
    root: Path,
    job_id: uuid.UUID,
    attempt: int,
    lease_token: uuid.UUID,
    *,
    create: bool = False,
) -> Path:
    if attempt < 0:
        raise ValueError("attempt must be non-negative")
    layout = ensure_staging_layout(root)
    path = layout.jobs / str(uuid.UUID(str(job_id))) / str(attempt) / str(uuid.UUID(str(lease_token)))
    if create:
        enqueue_staging_candidate(layout.jobs, path.relative_to(layout.jobs).as_posix())
        path.mkdir(parents=True, exist_ok=False)
    return path


def enqueue_staging_candidate(namespace_root: Path, relative_path: str) -> None:
    """Append one service-generated relative path to a bounded cleanup queue."""
    relative = Path(relative_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("invalid staging cleanup candidate")
    namespace_root = _absolute(namespace_root)
    _real_directory(namespace_root, "staging_namespace")
    with held_lock(namespace_root / ".cleanup-queue.lock") as owner:
        if owner is None:
            raise OSError("unable to acquire cleanup queue lock")
        with (namespace_root / ".cleanup-queue").open("a", encoding="ascii", newline="\n") as queue:
            queue.write(relative.as_posix() + "\n")
            queue.flush()
            os.fsync(queue.fileno())


@dataclass(frozen=True)
class SafeRemoveResult:
    status: Literal["deleted", "quarantined", "missing", "failed"]
    path: Path


def safe_remove_staging(path: Path, root: Path) -> SafeRemoveResult:
    """Quarantine then delete one contained staging child; never delete the root."""
    # Lazy import keeps the low-level deletion implementation in one place while
    # allowing cleanup.py to use the namespace/lock primitives above.
    from .cleanup import (
        UnsafeStagingPath,
        _inside,
        _lexical_path,
        _quarantine_and_remove,
        _tree_is_safe,
        _validated_staging_base,
    )

    base = _validated_staging_base(root)
    candidate = _lexical_path(path)
    if candidate == base or not _inside(candidate, base) or not _tree_is_safe(candidate, base):
        raise UnsafeStagingRoot("refusing_to_remove_unsafe_staging_path")
    try:
        status, outcome_path = _quarantine_and_remove(candidate, base)
        return SafeRemoveResult(status, outcome_path)
    except FileNotFoundError:
        return SafeRemoveResult("missing", candidate)
    except UnsafeStagingPath:
        return SafeRemoveResult("failed", candidate)
