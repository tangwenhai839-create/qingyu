import logging
import os
import sys
import threading
import time
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database_config import DatabaseConfigurationError
from ..errors import safe_log_exception
from ..logging_config import configure_logging

try:
    from .processor import ImportProcessor
    from .worker import ClaimedJob, ImportWorker, LeaseLost
except DatabaseConfigurationError as exc:
    if __name__ == "__main__":
        sys.stderr.write(f"worker_startup_failed code={exc} exception=DatabaseConfigurationError\n")
        raise SystemExit(2) from None
    raise

logger = logging.getLogger(__name__)


def cleanup_failed_staging(*args, **kwargs):
    """Lazy import preserves fail-closed startup diagnostics without DB side effects."""
    from .cleanup import cleanup_failed_staging as cleanup

    return cleanup(*args, **kwargs)


class AttemptHealth:
    """Thread-safe heartbeat failure signal without retaining sensitive errors."""
    def __init__(self):
        self.failed = threading.Event()

    def mark_failed(self) -> None:
        self.failed.set()

    def raise_if_failed(self) -> None:
        if self.failed.is_set():
            raise LeaseLost("heartbeat_failed")


class WorkerRuntime:
    def __init__(
        self,
        archive_root,
        poll_interval=1.0,
        heartbeat_interval=10.0,
        lease_duration=30,
        error_backoff_max=5.0,
        session_factory=None,
        staging_retention_days=7,
        cleanup_interval_seconds=300.0,
        cleanup_max_candidates=100,
        monotonic=None,
        repair_manifests_on_start=False,
    ):
        self.archive_root = archive_root
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.lease_duration = lease_duration
        self.error_backoff_max = error_backoff_max
        self.staging_retention_days = staging_retention_days
        self.cleanup_interval_seconds = cleanup_interval_seconds
        self.cleanup_max_candidates = cleanup_max_candidates
        self.monotonic = monotonic or time.monotonic
        if session_factory is None:
            from ..db import engine
            session_factory = lambda: Session(engine)
        self.session_factory = session_factory
        self.stop_event = threading.Event()
        self.last_iteration_failed = False
        self._next_cleanup_at = None
        self.repair_manifests_on_start = repair_manifests_on_start
        self._manifests_repaired = False

    def _maybe_run_cleanup(self) -> None:
        now = self.monotonic()
        if self._next_cleanup_at is not None and now < self._next_cleanup_at:
            return

        # Advance before cleanup so repeated failures cannot produce a tight loop.
        self._next_cleanup_at = now + self.cleanup_interval_seconds
        cleanup_db = None
        try:
            cleanup_db = self.session_factory()
            result = cleanup_failed_staging(
                self.archive_root,
                cleanup_db,
                retention_days=self.staging_retention_days,
                max_candidates=self.cleanup_max_candidates,
            )
            if result.counts["failed"] or result.counts.get("quarantined", 0):
                logger.warning(
                    "staging cleanup completed with retained failures",
                    extra={"cleanup_counts": result.counts},
                )
            elif result.counts["deleted"] or result.counts.get("quarantine_recovered", 0):
                logger.info("staging cleanup completed", extra={"cleanup_counts": result.counts})
        except Exception as exc:  # noqa: BLE001
            if cleanup_db is not None:
                try:
                    cleanup_db.rollback()
                except Exception:  # noqa: BLE001, S110
                    pass
            safe_log_exception(
                logger,
                logging.ERROR,
                "staging cleanup failed",
                error_code="staging_cleanup_failed",
                operation="cleanup_staging",
                exception=exc,
            )
        finally:
            if cleanup_db is not None:
                try:
                    cleanup_db.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    def run_once(self) -> bool:
        self.last_iteration_failed = False
        self._maybe_run_cleanup()
        db = self.session_factory()
        heartbeat_stop = None
        heartbeat = None
        operation = "recover_stale"
        try:
            worker = ImportWorker(db)
            worker.recover_stale()
            operation = "claim_next"
            claim = worker.claim_next(self.lease_duration, archive_root=self.archive_root)
            if claim is None:
                return False
            operation = "post_claim_setup"
            claim = self._prepare_claim(claim)
            heartbeat_stop = threading.Event()
            attempt_health = AttemptHealth()

            def heartbeat_loop():
                while not heartbeat_stop.wait(self.heartbeat_interval):
                    with self.session_factory() as heartbeat_db:
                        try:
                            alive = ImportWorker(heartbeat_db).heartbeat(claim.job_id, claim.lease_token, self.lease_duration)
                        except Exception as exc:  # noqa: BLE001
                            heartbeat_db.rollback()
                            attempt_health.mark_failed()
                            safe_log_exception(
                                logger,
                                logging.ERROR,
                                "import heartbeat failed",
                                error_code="heartbeat_failed",
                                operation="heartbeat",
                                exception=exc,
                                job_id=claim.job_id,
                            )
                            heartbeat_stop.set()
                            return
                        if not alive:
                            attempt_health.mark_failed()
                            heartbeat_stop.set()
                            return

            heartbeat = threading.Thread(target=heartbeat_loop, name=f"import-heartbeat-{claim.job_id}", daemon=True)
            operation = "heartbeat_setup"
            heartbeat.start()
            operation = "process_job"
            try:
                ImportProcessor(db, self.archive_root).process(claim, claim.lease_token, health_check=attempt_health.raise_if_failed)
            except LeaseLost:
                logger.info("import worker lost ownership", extra={"job_id": str(claim.job_id)})
            except SQLAlchemyError:
                raise
            except Exception as exc:  # noqa: BLE001
                safe_log_exception(
                    logger,
                    logging.ERROR,
                    "import job processing failed",
                    error_code="import_processing_failed",
                    operation="process_job",
                    exception=exc,
                    job_id=claim.job_id,
                )
            return True
        except SQLAlchemyError as exc:
            db.rollback()
            self.last_iteration_failed = True
            safe_log_exception(
                logger,
                logging.ERROR,
                "transient worker database failure",
                error_code="worker_database_unavailable",
                operation=operation,
                exception=exc,
            )
            return False
        finally:
            if heartbeat_stop is not None:
                heartbeat_stop.set()
            if heartbeat is not None:
                heartbeat.join(timeout=self.heartbeat_interval + 1)
            db.close()

    @staticmethod
    def _prepare_claim(claim: ClaimedJob) -> ClaimedJob:
        """Pure post-commit boundary hook; the claim contains no ORM state."""
        return claim

    def _repair_manifests_once(self) -> None:
        """Fill archive manifests that are missing. Existing files are kept.

        One attempt per process. A failure is logged and does not stop polling;
        the operator command is ``python -m scripts.maintenance repair-manifests``.
        """
        if not self.repair_manifests_on_start or self._manifests_repaired:
            return
        self._manifests_repaired = True
        db = None
        try:
            from sqlalchemy import select

            from ..archive.manifest import repair_missing_manifests
            from ..models import ArchiveObject

            db = self.session_factory()
            records = list(
                db.scalars(select(ArchiveObject).order_by(ArchiveObject.sha256, ArchiveObject.id))
            )
            written, kept = repair_missing_manifests(Path(self.archive_root), records)
            if written:
                logger.info(
                    "repaired missing archive manifests",
                    extra={"written": written, "kept": kept},
                )
        except Exception as exc:  # noqa: BLE001
            if db is not None:
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001, S110
                    pass
            safe_log_exception(
                logger,
                logging.ERROR,
                "manifest repair failed",
                error_code="manifest_repair_failed",
                operation="repair_manifests",
                exception=exc,
            )
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    def run_forever(self):
        self._repair_manifests_once()
        consecutive_failures = 0
        try:
            while not self.stop_event.is_set():
                try:
                    worked = self.run_once()
                except SQLAlchemyError as exc:
                    # Final safety net: a database error must never reach Python's
                    # default unhandled-exception printer.
                    self.last_iteration_failed = True
                    safe_log_exception(
                        logger,
                        logging.ERROR,
                        "worker iteration database failure",
                        error_code="worker_database_unavailable",
                        operation="run_once",
                        exception=exc,
                    )
                    worked = False
                if self.last_iteration_failed:
                    consecutive_failures += 1
                    delay = min(self.poll_interval * (2 ** (consecutive_failures - 1)), self.error_backoff_max)
                    self.stop_event.wait(delay)
                else:
                    consecutive_failures = 0
                    if not worked:
                        self.stop_event.wait(self.poll_interval)
        except KeyboardInterrupt:
            self.stop_event.set()


def _env_enabled(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    try:
        settings = get_settings()
        configure_logging(settings.log_level, settings.log_format)
        runtime = WorkerRuntime(
            archive_root=Path(settings.archive_root),
            poll_interval=float(os.getenv("IMPORT_WORKER_POLL_SECONDS", "1")),
            heartbeat_interval=float(os.getenv("IMPORT_WORKER_HEARTBEAT_SECONDS", "10")),
            lease_duration=int(os.getenv("IMPORT_WORKER_LEASE_SECONDS", "30")),
            staging_retention_days=settings.staging_failed_retention_days,
            cleanup_interval_seconds=settings.staging_cleanup_interval_seconds,
            cleanup_max_candidates=settings.staging_cleanup_max_candidates,
            repair_manifests_on_start=_env_enabled("IMPORT_WORKER_REPAIR_MANIFESTS", True),
        )
        runtime.run_forever()
        return 0
    except DatabaseConfigurationError as exc:
        sys.stderr.write(f"worker_startup_failed code={exc} exception=DatabaseConfigurationError\n")
        return 2
    except SQLAlchemyError as exc:
        sys.stderr.write(
            f"worker_startup_failed code=database_initialization_failed exception={exc.__class__.__name__}\n"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
