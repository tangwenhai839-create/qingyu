import os
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import ImportJob
from .staging import NAMESPACES, UnsafeStagingRoot, enqueue_staging_candidate, held_lock
from .status import ACTIVE_STATUSES, TERMINAL_STATUSES

TREE_ENTRY_BUDGET = 10000


@dataclass
class StagingCleanupResult:
    deleted: list[Path] = field(default_factory=list)
    skipped_active: list[Path] = field(default_factory=list)
    skipped_retention: list[Path] = field(default_factory=list)
    skipped_unknown: list[Path] = field(default_factory=list)
    quarantined: list[Path] = field(default_factory=list)
    quarantine_recovered: list[Path] = field(default_factory=list)
    failed: list[Path] = field(default_factory=list)
    _outcomes: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def record(self, outcome: str, path: Path) -> None:
        if outcome == "skipped_recent":
            outcome = "skipped_retention"
        path = _lexical_path(path)
        identity = os.path.normcase(str(path))
        if identity in self._outcomes:
            return
        self._outcomes[identity] = outcome
        getattr(self, outcome).append(path)

    @property
    def skipped_recent(self) -> list[Path]:
        """Backward-compatible name for the retention outcome."""
        return self.skipped_retention

    @property
    def counts(self) -> dict[str, int]:
        return {
            "deleted": len(self.deleted),
            "skipped_active": len(self.skipped_active),
            "skipped_retention": len(self.skipped_retention),
            "skipped_recent": len(self.skipped_retention),
            "skipped_unknown": len(self.skipped_unknown),
            "quarantined": len(self.quarantined),
            "quarantine_recovered": len(self.quarantine_recovered),
            "failed": len(self.failed),
        }


@dataclass(frozen=True)
class AttemptCleanupResult:
    outcome: str
    path: Path


def cleanup_failed_staging(
    root: Path,
    db: Session,
    retention_days: int = 7,
    dry_run: bool = False,
    protected_paths=None,
    max_candidates: int | None = None,
) -> StagingCleanupResult:
    """Compatibility entry point for the unified staging lifecycle scanner."""
    return cleanup_staging(root, db, retention_days, dry_run, protected_paths, max_candidates)


def cleanup_staging(
    root: Path,
    db: Session,
    retention_days: int = 7,
    dry_run: bool = False,
    protected_paths=None,
    max_candidates: int | None = None,
) -> StagingCleanupResult:
    """Clean abandoned pre-job generations and DB-fenced attempt staging."""
    result = StagingCleanupResult()
    if max_candidates is not None and max_candidates < 1:
        raise ValueError("max_candidates must be positive")
    budget = {"remaining": max_candidates}
    scan_budget = {"remaining": None if max_candidates is None else max(32, max_candidates * 32)}
    try:
        base = _validated_staging_base(root)
    except FileNotFoundError:
        return result
    except UnsafeStagingPath:
        result.record("skipped_unknown", _lexical_path(Path(root) / "staging"))
        return result
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    protected = {_identity(Path(path)) for path in (protected_paths or [])}
    # Leftover quarantine is always offered the first bounded cleanup slot,
    # without consuming the regular slot needed for eventual namespace progress.
    quarantine_budget = {"remaining": 1 if max_candidates is not None else None}
    _recover_quarantine(base, result, dry_run, quarantine_budget, scan_budget=scan_budget)
    for namespace in _rotating_namespaces(base, result):
        if budget["remaining"] == 0:
            break
        if namespace == "jobs":
            _cleanup_job_candidates(base, db, cutoff, protected, result, dry_run, budget, scan_budget)
        else:
            directory = base / namespace
            if directory.exists():
                _scan_prejob_generations(directory, base, cutoff, protected, result, dry_run, budget, scan_budget)
    return result


def _rotating_namespaces(base, result):
    names = ["jobs", "uploads", "archive-prepared"]
    lock_path = base / ".cleanup-cursor.lock"
    try:
        with held_lock(lock_path) as owner:
            if owner is None:
                return names
            owner.seek(0)
            raw = owner.read().decode("ascii", "ignore").strip() or "0"
            cursor = int(raw) % len(names) if raw.isdigit() else 0
            owner.seek(0); owner.truncate(); owner.write(str((cursor + 1) % len(names)).encode("ascii")); owner.flush()
    except (OSError, UnsafeStagingRoot):
        result.record("failed", lock_path)
        return names
    return names[cursor:] + names[:cursor]


def _cleanup_job_candidates(base, db, cutoff, protected, result, dry_run, budget, scan_budget):
    roots = []
    jobs_root = base / "jobs"
    if jobs_root.exists():
        roots.append(jobs_root)
    roots.append(base)  # legacy layout
    for tree_root in roots:
        queued = None if tree_root == base else _reserve_queued_paths(
            tree_root, result, budget["remaining"]
        )
        candidates = (
            _parse_queued_job_paths(base, tree_root, queued, result)
            if queued is not None
            else _scan_job_tree(base, tree_root, result, legacy=tree_root == base, scan_budget=scan_budget)
        )
        for path, job_id, attempt, token in candidates:
            if not os.path.lexists(path):
                continue
            if not _take_budget(budget):
                return
            if _identity(path) in protected:
                result.record("skipped_active", path); continue
            try:
                safe_tree = _candidate_preflight(path, base)
            except OSError:
                result.record("failed", path); continue
            if not safe_tree:
                result.record("skipped_unknown", path); continue
            preliminary = _classify_attempt(db, job_id, attempt, token)
            if preliminary != "deletable":
                result.record(preliminary, path); continue
            try:
                modified = datetime.fromtimestamp(os.lstat(path).st_mtime, UTC)
            except OSError:
                result.record("failed", path); continue
            if modified >= cutoff:
                result.record("skipped_retention", path); continue
            if dry_run:
                result.record("deleted", path); continue
            try:
                outcome, outcome_path = _delete_attempt_with_fence(db, path, base, job_id, attempt, token)
                if outcome is not None:
                    result.record("deleted" if outcome == "deletable" else outcome, outcome_path)
            except UnsafeStagingPath:
                result.record("skipped_unknown", path)
            except OSError:
                result.record("failed", path)


def cleanup_terminal_attempt(
    root: Path,
    db: Session,
    job_id: uuid.UUID,
    attempt: int,
    lease_token: uuid.UUID,
) -> AttemptCleanupResult:
    """Immediately clean one terminal attempt with a final locked DB fence."""
    from .staging import job_attempt_path

    path = _lexical_path(job_attempt_path(root, job_id, attempt, lease_token))
    try:
        base = _validated_staging_base(root)
    except (FileNotFoundError, UnsafeStagingPath):
        return AttemptCleanupResult("skipped_unknown", path)
    if not os.path.lexists(path):
        return AttemptCleanupResult("missing", path)
    try:
        if not _candidate_preflight(path, base):
            return AttemptCleanupResult("skipped_unknown", path)
    except OSError:
        return AttemptCleanupResult("failed", path)
    try:
        outcome, outcome_path = _delete_attempt_with_fence(
            db, path, base, uuid.UUID(str(job_id)), attempt, uuid.UUID(str(lease_token))
        )
        if outcome is None:
            return AttemptCleanupResult("missing", path)
        if outcome == "deletable":
            outcome = "deleted"
        return AttemptCleanupResult(outcome, outcome_path)
    except UnsafeStagingPath:
        return AttemptCleanupResult("skipped_unknown", path)
    except OSError:
        return AttemptCleanupResult("failed", path)


def _scan_job_tree(base: Path, jobs_root: Path, result: StagingCleanupResult, legacy=False, scan_budget=None):
    for job_dir in _children(jobs_root, result, scan_budget):
        if legacy and (job_dir.name in NAMESPACES or job_dir.name.startswith(".cleanup-")):
            continue
        if _is_link_like(job_dir) or not _is_directory(job_dir):
            result.record("skipped_unknown", job_dir); continue
        try:
            job_id = uuid.UUID(job_dir.name)
        except ValueError:
            result.record("skipped_unknown", job_dir); continue
        for leaf, attempt, token in _attempt_leaves(base, job_dir, result, scan_budget):
            yield leaf, job_id, attempt, token


def _take_budget(budget):
    if budget["remaining"] is None:
        return True
    if budget["remaining"] == 0:
        return False
    budget["remaining"] -= 1
    return True


def _scan_prejob_generations(parent, base, cutoff, protected, result, dry_run, budget, scan_budget):
    if _is_link_like(parent) or not _is_directory(parent):
        result.record("skipped_unknown", parent); return
    try:
        namespace_guard = held_lock(parent / ".namespace.lock")
        namespace_owner = namespace_guard.__enter__()
    except (OSError, UnsafeStagingRoot):
        result.record("failed", parent); return
    try:
        if namespace_owner is None:
            result.record("skipped_active", parent); return
        queued = _reserve_queued_paths(parent, result, budget["remaining"])
        paths = queued if queued is not None else _children(parent, result, scan_budget)
        for path in paths:
            if path.name.startswith("."):
                continue
            if not os.path.lexists(path):
                continue
            if not _take_budget(budget):
                return
            if _identity(path) in protected:
                result.record("skipped_active", path); continue
            if _is_link_like(path) or not _is_directory(path):
                result.record("skipped_unknown", path); continue
            try:
                uuid.UUID(path.name)
            except ValueError:
                result.record("skipped_unknown", path); continue
            owner_path = path / ".owner.lock"
            if not owner_path.exists() or _is_link_like(owner_path):
                if _abandoned_generation_is_eligible(path, base, cutoff, result, dry_run):
                    _cleanup_abandoned_generation(path, base, result)
                continue
            try:
                with held_lock(owner_path, blocking=False) as generation_owner:
                    if generation_owner is None:
                        result.record("skipped_active", path); continue
                    eligible = _abandoned_generation_is_eligible(
                        path, base, cutoff, result, dry_run
                    )
                # Windows cannot rename a directory containing a locked child
                # file. The namespace lock is still held here, and every creator
                # publishes its owner lock under that same fence, so releasing
                # this abandoned owner handle cannot admit a new producer.
                if eligible:
                    _cleanup_abandoned_generation(path, base, result)
            except (OSError, UnsafeStagingRoot):
                result.record("failed", path)
    finally:
        namespace_guard.__exit__(None, None, None)


def _abandoned_generation_is_eligible(path, base, cutoff, result, dry_run):
    try:
        if not _candidate_preflight(path, base):
            result.record("skipped_unknown", path)
            return False
    except OSError:
        result.record("failed", path)
        return False
    try:
        modified = datetime.fromtimestamp(os.lstat(path).st_mtime, UTC)
    except OSError:
        result.record("failed", path); return False
    if modified >= cutoff:
        result.record("skipped_retention", path); return False
    if dry_run:
        result.record("deleted", path); return False
    return True


def _cleanup_abandoned_generation(path, base, result):
    try:
        quarantine = _move_to_quarantine(path, base)
    except FileNotFoundError:
        return
    except OSError as exc:
        if _is_windows_busy(exc):
            result.record("skipped_active", path)
        else:
            result.record("failed", path)
        return
    try:
        _remove_tree_without_links(quarantine)
        result.record("deleted", path)
    except (OSError, UnsafeStagingPath):
        result.record("quarantined", quarantine)


def _classify_attempt(db: Session, job_id, attempt, token):
    job = db.scalar(
        select(ImportJob).where(ImportJob.id == job_id).execution_options(populate_existing=True)
    )
    database_now = db.scalar(select(func.current_timestamp()))
    if job is None:
        return "deletable"
    if _has_active_ownership(job, attempt, token, database_now):
        return "skipped_active"
    if _has_live_lease(job, database_now):
        return "skipped_unknown"
    if job.status in ACTIVE_STATUSES:
        return "skipped_active"
    if job.status not in TERMINAL_STATUSES:
        return "skipped_unknown"
    return "deletable"


def _delete_attempt_with_fence(db, path, base, job_id, attempt, token):
    # A separate short transaction releases the row lock before scanning the next
    # candidate, even when the caller supplied a long-lived Session.
    quarantine = None
    with Session(db.get_bind()) as fence_db, fence_db.begin():
        job = fence_db.scalar(
            select(ImportJob)
            .where(ImportJob.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        database_now = fence_db.scalar(select(func.current_timestamp()))
        if job is not None:
            if _has_active_ownership(job, attempt, token, database_now):
                return "skipped_active", path
            if _has_live_lease(job, database_now):
                return "skipped_unknown", path
            if job.status in ACTIVE_STATUSES:
                return "skipped_active", path
            if job.status not in TERMINAL_STATUSES:
                return "skipped_unknown", path
        if not os.path.lexists(path):
            return None, path
        # The bounded full preflight already ran before this transaction. Keep
        # the row-lock window to the final shallow boundary check + rename.
        if not _path_boundary_is_safe(path, base):
            return "skipped_unknown", path
        try:
            quarantine = _move_to_quarantine(path, base)
        except FileNotFoundError:
            return None, path
    # Recursive I/O deliberately happens after releasing the ImportJob row lock.
    try:
        _remove_tree_without_links(quarantine)
        return "deletable", path
    except FileNotFoundError:
        return None, path
    except (OSError, UnsafeStagingPath):
        return "quarantined", quarantine


def _recover_quarantine(base, result, dry_run, budget, candidates=None, scan_budget=None):
    quarantine_root = base / "quarantine"
    if not quarantine_root.exists():
        return
    if _is_link_like(quarantine_root) or not _is_directory(quarantine_root):
        result.record("skipped_unknown", quarantine_root); return
    current_quarantine = {_identity(path) for path in result.quarantined}
    # Take a bounded snapshot so a claim name created by this pass cannot be
    # rediscovered and raced within the same directory enumeration.
    queued = _reserve_queued_paths(quarantine_root, result, budget["remaining"])
    paths = (
        queued
        if queued is not None
        else list(_children(quarantine_root, result, scan_budget)) if candidates is None else candidates
    )
    for path in paths:
        if _identity(path) in current_quarantine:
            continue
        if not os.path.lexists(path):
            continue
        if not _take_budget(budget):
            return
        if _is_link_like(path) or not _is_directory(path):
            result.record("skipped_unknown", path); continue
        try:
            _quarantine_identity(path.name)
        except ValueError:
            result.record("skipped_unknown", path); continue
        try:
            safe_tree = _candidate_preflight(path, base)
        except OSError:
            result.record("failed", path); continue
        if not safe_tree:
            result.record("skipped_unknown", path); continue
        if dry_run:
            result.record("quarantine_recovered", path); continue
        try:
            claimed = quarantine_root / f".claim-{uuid.uuid4()}"
            os.replace(path, claimed)
        except FileNotFoundError:
            continue
        except OSError as exc:
            if not os.path.lexists(path):
                continue
            if _is_windows_busy(exc):
                result.record("skipped_active", path)
            else:
                result.record("failed", path)
            continue
        try:
            _remove_claim_after_concurrent_scan(claimed)
            result.record("quarantine_recovered", path)
        except FileNotFoundError:
            continue
        except UnsafeStagingPath:
            result.record("skipped_unknown", path)
        except OSError:
            if os.path.lexists(claimed):
                result.record("failed", path)


def _quarantine_identity(name: str) -> uuid.UUID:
    """Accept normal quarantine entries and restart-left private claims."""
    return uuid.UUID(name.removeprefix(".claim-"))


def _reserve_queued_paths(parent: Path, result: StagingCleanupResult, limit: int | None):
    """Reserve a fair, bounded slice from a service-owned append-only queue."""
    queue_path = parent / ".cleanup-queue"
    if not queue_path.exists():
        return None
    record_limit = limit if limit is not None else 10000
    if record_limit <= 0:
        return []
    lock_path = parent / ".cleanup-queue.lock"
    cursor_path = parent / ".cleanup-queue.cursor"
    try:
        with held_lock(lock_path) as owner:
            if owner is None:
                return []
            try:
                offset = int(cursor_path.read_text("ascii")) if cursor_path.exists() else 0
            except (OSError, ValueError):
                offset = 0
            selected = []
            seen_offsets = set()
            with queue_path.open("r", encoding="ascii") as queue:
                queue.seek(offset)
                wrapped = False
                while len(selected) < record_limit:
                    position = queue.tell()
                    if position in seen_offsets:
                        break
                    seen_offsets.add(position)
                    line = queue.readline()
                    if not line:
                        if wrapped:
                            break
                        queue.seek(0); wrapped = True; continue
                    raw = line.strip()
                    if not raw or len(raw) > 512:
                        continue
                    relative = Path(raw)
                    candidate = _lexical_path(parent / relative)
                    if relative.is_absolute() or not _inside(candidate, parent):
                        result.record("skipped_unknown", queue_path)
                        continue
                    selected.append(candidate)
                next_offset = queue.tell()
            cursor_path.write_text(str(next_offset), encoding="ascii")
            return selected
    except (OSError, UnsafeStagingRoot):
        result.record("failed", queue_path)
        return []


def _parse_queued_job_paths(base, jobs_root, paths, result):
    for path in paths:
        try:
            relative = path.relative_to(jobs_root)
            if len(relative.parts) != 3:
                raise ValueError
            job_id = uuid.UUID(relative.parts[0])
            attempt = int(relative.parts[1])
            token = uuid.UUID(relative.parts[2])
        except (ValueError, TypeError):
            result.record("skipped_unknown", path)
            continue
        yield path, job_id, attempt, token


def _remove_claim_after_concurrent_scan(path: Path) -> None:
    """Retry brief Windows handle races; persistent I/O errors stay visible."""
    for attempt in range(4):
        try:
            _remove_tree_without_links(path)
            return
        except OSError as exc:
            transient_guard = os.name == "nt" and "directory_identity_guard_failed" in str(exc)
            if not transient_guard or attempt == 3:
                raise
            time.sleep(0.01)


class UnsafeStagingPath(RuntimeError):
    pass


class CleanupBudgetExhausted(OSError):
    pass


def _lexical_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _identity(path: Path) -> str:
    return os.path.normcase(str(_lexical_path(path)))


def _validated_staging_base(root: Path) -> Path:
    archive_root = _lexical_path(Path(root))
    if not os.path.lexists(archive_root) or _is_link_like(archive_root) or not _is_directory(archive_root):
        raise UnsafeStagingPath("unsafe_archive_root")
    base = _lexical_path(archive_root / "staging")
    if not os.path.lexists(base):
        raise FileNotFoundError(base)
    if _is_link_like(base) or not _is_directory(base):
        raise UnsafeStagingPath("unsafe_staging_base")
    try:
        resolved_root = archive_root.resolve(strict=True)
        resolved_base = base.resolve(strict=True)
        resolved_base.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise UnsafeStagingPath("unresolvable_staging_base") from exc
    return base


def _is_link_like(path: Path) -> bool:
    try:
        details = os.lstat(path)
    except OSError:
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = getattr(details, "st_file_attributes", 0)
    return stat.S_ISLNK(details.st_mode) or bool(attributes & reparse_flag) or os.path.ismount(path)


def _is_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _children(path: Path, result: StagingCleanupResult | None = None, scan_budget=None):
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if scan_budget is not None and not _take_budget(scan_budget):
                    return
                yield _lexical_path(Path(entry.path))
    except OSError:
        if result is not None:
            result.record("failed", path)


def _inside(path: Path, base: Path) -> bool:
    try:
        _lexical_path(path).relative_to(_lexical_path(base))
        return True
    except ValueError:
        return False


def _is_windows_busy(exc: OSError) -> bool:
    return os.name == "nt" and getattr(exc, "winerror", None) in {32, 33}


def _has_active_ownership(job: ImportJob, attempt: int, token: uuid.UUID, database_now: datetime) -> bool:
    return (
        job.lease_token is not None
        and job.lease_expires_at is not None
        and job.attempt_count == attempt
        and job.lease_token == token
        and job.lease_expires_at > database_now
    )


def _has_live_lease(job: ImportJob, database_now: datetime) -> bool:
    return (
        job.lease_token is not None
        and job.lease_expires_at is not None
        and job.lease_expires_at > database_now
    )


def _tree_is_safe(candidate: Path, base: Path, *, max_nodes: int | None = None) -> bool:
    if max_nodes is None:
        max_nodes = TREE_ENTRY_BUDGET
    candidate = _lexical_path(candidate)
    base = _lexical_path(base)
    if not _inside(candidate, base):
        return False
    current = candidate
    while current != base:
        if _is_link_like(current):
            return False
        current = current.parent
    stack = [candidate]
    visited = 0
    while stack:
        current = stack.pop()
        visited += 1
        if visited > max_nodes:
            raise CleanupBudgetExhausted("staging_tree_scan_budget_exhausted")
        if not _inside(current, base) or _is_link_like(current):
            return False
        with os.scandir(current) as entries:
            for entry in entries:
                visited += 1
                if visited > max_nodes:
                    raise CleanupBudgetExhausted("staging_tree_scan_budget_exhausted")
                child = _lexical_path(Path(entry.path))
                if not _inside(child, candidate) or _is_link_like(child):
                    return False
                if entry.is_dir(follow_symlinks=False):
                    stack.append(child)
    try:
        resolved_base = base.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=True)
        resolved_candidate.relative_to(resolved_base)
    except (OSError, ValueError):
        return False
    return True


def _candidate_preflight(candidate: Path, base: Path) -> bool:
    """Fully inspect small trees; large safe-boundary trees proceed to quarantine."""
    try:
        return _tree_is_safe(candidate, base)
    except CleanupBudgetExhausted:
        return _path_boundary_is_safe(candidate, base)


def _path_boundary_is_safe(candidate: Path, base: Path) -> bool:
    candidate = _lexical_path(candidate)
    base = _lexical_path(base)
    if candidate == base or not _inside(candidate, base):
        return False
    current = candidate
    while current != base:
        if _is_link_like(current):
            return False
        current = current.parent
    resolved_candidate = candidate.resolve(strict=True)
    resolved_candidate.relative_to(base.resolve(strict=True))
    return True


def _restore_quarantine(quarantine: Path, original: Path) -> None:
    if os.path.lexists(quarantine) and not os.path.lexists(original):
        try:
            os.replace(quarantine, original)
        except OSError:
            pass


def _quarantine_and_remove(path: Path, base: Path) -> tuple[str, Path]:
    quarantine = _move_to_quarantine(path, base)
    try:
        _remove_tree_without_links(quarantine)
        return "deleted", path
    except (OSError, UnsafeStagingPath):
        # Never move a partially deleted tree back into a live namespace.  The
        # formal quarantine name is restart-discoverable and safe to retry.
        return "quarantined", quarantine


def _move_to_quarantine(path: Path, base: Path) -> Path:
    base = _validated_staging_base(base.parent)
    path = _lexical_path(path)
    if not _path_boundary_is_safe(path, base):
        raise UnsafeStagingPath("unsafe_delete_candidate")
    quarantine_root = _lexical_path(base / "quarantine")
    quarantine_root.mkdir(exist_ok=True)
    if _is_link_like(quarantine_root) or not _is_directory(quarantine_root):
        raise UnsafeStagingPath("unsafe_quarantine_root")
    quarantine = quarantine_root / str(uuid.uuid4())
    enqueue_staging_candidate(quarantine_root, quarantine.name)
    os.replace(path, quarantine)
    try:
        safe_quarantine = _path_boundary_is_safe(quarantine, base)
    except OSError:
        _restore_quarantine(quarantine, path)
        raise
    if not safe_quarantine:
        _restore_quarantine(quarantine, path)
        raise UnsafeStagingPath("unsafe_after_quarantine")
    return quarantine


def _remove_tree_without_links(path: Path, _budget=None) -> None:
    if _budget is None:
        _budget = {"remaining": TREE_ENTRY_BUDGET}
    if _budget["remaining"] <= 0:
        raise CleanupBudgetExhausted("staging_tree_delete_budget_exhausted")
    _budget["remaining"] -= 1
    with _directory_identity_guard(path):
        if _is_link_like(path):
            raise UnsafeStagingPath("link_like_path")
        while True:
            with os.scandir(path) as entries:
                entry = next(entries, None)
            if entry is None:
                break
            if _budget["remaining"] <= 0:
                raise CleanupBudgetExhausted("staging_tree_delete_budget_exhausted")
            _budget["remaining"] -= 1
            child = Path(entry.path)
            if _is_link_like(child):
                raise UnsafeStagingPath("link_like_entry")
            if _is_directory(child):
                quarantine = child.with_name(f".{child.name}.cleanup-{uuid.uuid4()}")
                os.replace(child, quarantine)
                try:
                    _remove_tree_without_links(quarantine, _budget)
                except Exception:
                    _restore_quarantine(quarantine, child)
                    raise
            else:
                os.unlink(child)
    os.rmdir(path)


@contextmanager
def _directory_identity_guard(path: Path):
    """Prevent a Windows directory path from being replaced while traversed."""
    if os.name != "nt":
        yield
        return

    import ctypes
    from ctypes import wintypes

    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        0x10080,  # DELETE | FILE_READ_ATTRIBUTES
        0x1 | 0x2,  # FILE_SHARE_READ | FILE_SHARE_WRITE; deliberately no DELETE
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    invalid_handle = wintypes.HANDLE(-1).value
    if handle in (None, 0, invalid_handle):
        raise OSError(ctypes.get_last_error(), "directory_identity_guard_failed")
    try:
        yield
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _attempt_leaves(base: Path, job_dir: Path, result: StagingCleanupResult, scan_budget=None):
    for attempt_dir in _children(job_dir, result, scan_budget):
        if _is_link_like(attempt_dir) or not _is_directory(attempt_dir) or not attempt_dir.name.isdigit():
            result.record("skipped_unknown", attempt_dir)
            continue
        attempt = int(attempt_dir.name)
        for lease_dir in _children(attempt_dir, result, scan_budget):
            path = _lexical_path(lease_dir)
            if _is_link_like(lease_dir) or not _is_directory(lease_dir) or not _inside(path, base):
                result.record("skipped_unknown", path)
                continue
            try:
                token = uuid.UUID(lease_dir.name)
            except ValueError:
                result.record("skipped_unknown", path)
                continue
            yield path, attempt, token
