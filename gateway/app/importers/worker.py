import copy
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from ..archive.manifest import sync_manifest_from_archive_object
from ..models import ArchiveObject, AttachmentObservation, ImportBatchCommit, ImportJob
from .report import empty_report
from .staging import job_attempt_path
from .status import COMPLETED, FAILED, PARTIAL, PENDING, RETRYABLE_STATUSES, RUNNING


@dataclass(frozen=True)
class ClaimedJob:
    """Commit-stable Worker claim data; accessing it can never issue SQL."""
    job_id: uuid.UUID
    lease_token: uuid.UUID
    attempt_count: int
    source_account_id: uuid.UUID
    archive_object_id: uuid.UUID

    @property
    def id(self):
        return self.job_id


@dataclass(frozen=True)
class DurableJobState:
    batches: tuple[ImportBatchCommit, ...]
    counts: dict
    report: dict


class DurableLedgerInconsistent(RuntimeError):
    pass

class ImportWorker:
    def __init__(self, db: Session): self.db = db
    def _fresh_session(self) -> Session:
        return sessionmaker(bind=self.db.get_bind(), autoflush=False, expire_on_commit=False)()
    def recover_stale(self) -> int:
        observed = self.db.execute(select(ImportJob.id, ImportJob.lease_token, ImportJob.lease_expires_at, ImportJob.attempt_count, ImportJob.max_attempts).where(ImportJob.status == RUNNING, ImportJob.lease_expires_at <= database_wall_clock())).all()
        self.db.rollback(); recovered = 0
        for job_id, token, expiry, attempts, maximum in observed:
            recovered += int(self.recover_observed(job_id, token, expiry, attempts, maximum))
        return recovered

    def recover_observed(self, job_id, token, expiry, attempts, maximum) -> bool:
        with self._fresh_session() as lifecycle, lifecycle.begin():
            owned = lifecycle.scalar(
                select(ImportJob)
                .where(
                    ImportJob.id == job_id,
                    ImportJob.status == RUNNING,
                    ImportJob.lease_token == token,
                    ImportJob.lease_expires_at == expiry,
                    ImportJob.lease_expires_at <= database_wall_clock(),
                    ImportJob.attempt_count == attempts,
                    ImportJob.max_attempts == maximum,
                )
                .with_for_update()
            )
            if owned is None:
                return False
            owned.lease_token = None
            owned.lease_expires_at = None
            if owned.attempt_count < owned.max_attempts:
                owned.status = PENDING
                owned.error_summary = "retry_after_stale: lease_expired"
                return True

            snapshot = aggregate_durable_job_state(
                lifecycle,
                job_id,
                owned.attempt_count,
                terminal_report=owned.report_json,
            )
            terminal = PARTIAL if snapshot.batches else FAILED
            owned.status = terminal
            owned.result_code = terminal
            owned.error_code = "lease_expired"
            owned.finished_at = database_wall_clock()
            owned.error_summary = "worker_crash: lease_expired"
            owned.report_json = snapshot.report
            apply_durable_counts(owned, snapshot)
            return True
    def claim_next(self, lease_duration: int = 300) -> ClaimedJob | None:
        job = self.db.scalar(
            select(ImportJob)
            .where(
                ImportJob.status == PENDING,
                ImportJob.attempt_count < ImportJob.max_attempts,
            )
            .order_by(ImportJob.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if job is None: return None
        now = datetime.now(UTC)
        token = uuid.uuid4()
        job.status = RUNNING; job.started_at = job.started_at or now; job.first_started_at = job.first_started_at or now; job.last_attempt_started_at = now; job.attempt_count += 1; job.last_heartbeat_at = now; job.lease_expires_at = now + timedelta(seconds=lease_duration); job.lease_token = token
        self.db.flush()
        claimed = ClaimedJob(
            job_id=job.id,
            lease_token=token,
            attempt_count=job.attempt_count,
            source_account_id=job.source_account_id,
            archive_object_id=job.archive_object_id,
        )
        self.db.commit()
        return claimed
    def _owned(self, job, token, values: dict) -> bool:
        job_id = getattr(job, "id", job)
        values = {k: (database_wall_clock() if k in {"last_heartbeat_at", "finished_at"} else v) for k, v in values.items()}
        with self._fresh_session() as lifecycle:
            result = lifecycle.execute(update(ImportJob).where(ownership_predicate(job_id, token)).values(**values))
            if result.rowcount != 1:
                lifecycle.rollback()
                return False
            lifecycle.commit()
            return True

    def assert_ownership(self, job, token):
        if token is None: raise LeaseRequired("lease_required")
        current = self.db.scalar(select(ImportJob).where(ownership_predicate(job.id, token)).with_for_update())
        if current is None:
            self.db.rollback()
            raise LeaseLost("ownership_lost")
        return current

    def check_ownership(self, job, token) -> None:
        job_id = getattr(job, "id", job)
        if token is None: raise LeaseRequired("lease_required")
        with self._fresh_session() as lifecycle:
            if lifecycle.scalar(select(ImportJob.id).where(ownership_predicate(job_id, token))) is None:
                raise LeaseLost("ownership_lost")

    def attempt_staging_path(self, root, job) -> str:
        self.check_ownership(job, job.lease_token)
        return str(job_attempt_path(root, job.id, job.attempt_count, job.lease_token))
    def heartbeat(self, job, token, lease_duration: int = 300) -> bool:
        return self._owned(job, token, {"last_heartbeat_at": database_wall_clock(), "lease_expires_at": database_wall_clock() + timedelta(seconds=lease_duration)})
    def advance_checkpoint(self, job, token, sequence_no: int, batch_key: str) -> bool:
        job_id = getattr(job, "id", job)
        checkpoint = {"committed_batches": sequence_no, "last_batch_key": batch_key}
        durable_batch = exists(
            select(ImportBatchCommit.id).where(
                ImportBatchCommit.import_job_id == job_id,
                ImportBatchCommit.sequence_no == sequence_no,
                ImportBatchCommit.batch_key == batch_key,
            )
        )
        with self._fresh_session() as lifecycle:
            result = lifecycle.execute(
                update(ImportJob)
                .where(
                    ownership_predicate(job_id, token),
                    ImportJob.checkpoint_sequence < sequence_no,
                    durable_batch,
                )
                .values(checkpoint_sequence=sequence_no, checkpoint=checkpoint)
            )
            if result.rowcount == 1:
                lifecycle.commit()
                return True
            lifecycle.rollback()
        with self._fresh_session() as lifecycle:
            current = lifecycle.scalar(
                select(ImportJob.checkpoint_sequence).where(ownership_predicate(job_id, token))
            )
            return current is not None and current >= sequence_no

    def update_progress(self, job, token, checkpoint: dict) -> bool:
        sequence = checkpoint.get("committed_batches")
        if not isinstance(sequence, int) or sequence < 0:
            raise ValueError("invalid_checkpoint_sequence")
        return self.advance_checkpoint(job, token, sequence, checkpoint.get("last_batch_key", ""))
    def mark_completed(self, job, token, report: dict | None = None, result_code: str = "imported", counts: dict | None = None) -> bool:
        job_id = getattr(job, "id", job)
        with self._fresh_session() as lifecycle, lifecycle.begin():
            owned = lifecycle.scalar(select(ImportJob).where(ownership_predicate(job_id, token)).with_for_update())
            if owned is None:
                return False
            snapshot = aggregate_durable_job_state(
                lifecycle, job_id, owned.attempt_count, terminal_report=report
            )
            owned.status = COMPLETED
            owned.finished_at = database_wall_clock()
            owned.report_json = snapshot.report
            owned.result_code = result_code
            owned.lease_token = None
            owned.lease_expires_at = None
            if snapshot.batches:
                apply_durable_counts(owned, snapshot)
            else:
                for key, value in (counts or {}).items():
                    if key in snapshot.counts and value != snapshot.counts[key]:
                        raise DurableLedgerInconsistent("inconsistent_terminal_counts")
                apply_durable_counts(owned, snapshot)
        return True
    def mark_failed(self, job, token, error: str, report: dict | None = None, result_code: str | None = None) -> bool:
        job_id = getattr(job, "id", job)
        with self._fresh_session() as lifecycle, lifecycle.begin():
            owned = lifecycle.scalar(
                select(ImportJob).where(ownership_predicate(job_id, token)).with_for_update()
            )
            if owned is None:
                return False
            snapshot = aggregate_durable_job_state(
                lifecycle,
                job_id,
                owned.attempt_count,
                terminal_report=report,
            )
            terminal = PARTIAL if snapshot.batches else FAILED
            owned.status = terminal
            owned.finished_at = database_wall_clock()
            owned.error_summary = error
            owned.report_json = snapshot.report
            owned.error_code = error
            owned.result_code = result_code or terminal
            owned.lease_token = None
            owned.lease_expires_at = None
            apply_durable_counts(owned, snapshot)
        return True

    def mark_failed_or_partial(self, job, token, error: str, report: dict | None = None, result_code: str | None = None) -> str:
        job_id = getattr(job, "id", job)
        with self._fresh_session() as lifecycle, lifecycle.begin():
            owned = lifecycle.scalar(select(ImportJob).where(ownership_predicate(job_id, token)).with_for_update())
            if owned is None:
                raise LeaseLost("ownership_lost")
            snapshot = aggregate_durable_job_state(
                lifecycle,
                job_id,
                owned.attempt_count,
                terminal_report=report,
            )
            terminal = PARTIAL if snapshot.batches else FAILED
            owned.status = terminal
            owned.finished_at = database_wall_clock()
            owned.error_summary = error
            owned.report_json = snapshot.report
            owned.error_code = error
            owned.result_code = result_code or terminal
            owned.lease_token = None
            owned.lease_expires_at = None
            apply_durable_counts(owned, snapshot)
        return terminal

    def retry(
        self,
        job_id,
        expected_parser_name: str | None = None,
        expected_parser_version: str | None = None,
    ) -> bool:
        if (expected_parser_name is None) != (expected_parser_version is None):
            raise ValueError("parser identity must include both name and version")
        ledger_exists = exists(
            select(ImportBatchCommit.id).where(
                ImportBatchCommit.import_job_id == ImportJob.id
            )
        )
        retry_conditions = [
            ImportJob.id == job_id,
            ImportJob.status.in_(RETRYABLE_STATUSES),
            ImportJob.lease_token.is_(None),
            ImportJob.attempt_count < ImportJob.max_attempts,
            ImportJob.error_code.is_distinct_from("archive_integrity_failure"),
            ImportJob.result_code.is_distinct_from("archive_integrity_failure"),
            exists(
                select(ArchiveObject.id).where(
                    ArchiveObject.id == ImportJob.archive_object_id,
                    ArchiveObject.integrity_status == "verified",
                )
            ),
        ]
        if expected_parser_name is None:
            # A direct caller cannot bypass parser pinning for any partial
            # replay, even if its ledger is unexpectedly empty.
            retry_conditions.extend((~ledger_exists, ImportJob.status != PARTIAL))
        else:
            matching_ledger = exists(
                select(ImportBatchCommit.id).where(
                    ImportBatchCommit.import_job_id == ImportJob.id,
                    ImportBatchCommit.parser_name == expected_parser_name,
                    ImportBatchCommit.contract_version == expected_parser_version,
                )
            )
            conflicting_ledger = exists(
                select(ImportBatchCommit.id).where(
                    ImportBatchCommit.import_job_id == ImportJob.id,
                    or_(
                        ImportBatchCommit.parser_name != expected_parser_name,
                        ImportBatchCommit.contract_version != expected_parser_version,
                    ),
                )
            )
            retry_conditions.append(
                or_(
                    and_(
                        ~ledger_exists,
                        or_(
                            ImportJob.status != PARTIAL,
                            and_(
                                ImportJob.parser_name == expected_parser_name,
                                ImportJob.parser_version == expected_parser_version,
                            ),
                        ),
                    ),
                    and_(
                        ImportJob.parser_name == expected_parser_name,
                        ImportJob.parser_version == expected_parser_version,
                        matching_ledger,
                        ~conflicting_ledger,
                    ),
                )
            )
        values = {
            "status": PENDING,
            "result_code": None,
            "error_code": None,
            "error_summary": None,
            "finished_at": None,
            "lease_expires_at": None,
        }
        if expected_parser_name is not None:
            # Ledger-free failed jobs adopt the parser selected by preflight;
            # durable jobs simply re-assert their already-pinned identity.
            values.update(
                parser_name=expected_parser_name,
                parser_version=expected_parser_version,
            )
        with self._fresh_session() as lifecycle:
            result = lifecycle.execute(
                update(ImportJob)
                .where(*retry_conditions)
                .values(**values)
            )
            if result.rowcount != 1:
                lifecycle.rollback()
                return False
            lifecycle.commit()
            return True

    def mark_archive_integrity_failure(
        self,
        *,
        job_id,
        token,
        archive_object_id,
        archive_root: Path,
        integrity_error: str,
        report: dict,
    ) -> None:
        with self._fresh_session() as lifecycle:
            with lifecycle.begin():
                owned_job = lifecycle.scalar(
                    select(ImportJob)
                    .where(ownership_predicate(job_id, token))
                    .with_for_update()
                )
                if owned_job is None:
                    raise LeaseLost("ownership_lost")
                archive = lifecycle.scalar(
                    select(ArchiveObject)
                    .where(ArchiveObject.id == archive_object_id)
                    .with_for_update()
                )
                if archive is None:
                    raise ValueError("archive_object_missing")
                archive.integrity_status = "integrity_failure"
                archive.integrity_error = integrity_error
                archive.integrity_checked_at = database_wall_clock()
                snapshot = aggregate_durable_job_state(
                    lifecycle,
                    job_id,
                    owned_job.attempt_count,
                    terminal_report=report,
                )
                terminal = PARTIAL if snapshot.batches else FAILED
                owned_job.status = terminal
                owned_job.result_code = "archive_integrity_failure"
                owned_job.error_code = "archive_integrity_failure"
                owned_job.error_summary = "archive_integrity_failure"
                owned_job.report_json = snapshot.report
                owned_job.finished_at = database_wall_clock()
                owned_job.lease_token = None
                owned_job.lease_expires_at = None
                apply_durable_counts(owned_job, snapshot)

            archive = lifecycle.get(ArchiveObject, archive_object_id)
            lifecycle.refresh(archive)
            sync_manifest_from_archive_object(archive_root, archive)

class LeaseLost(RuntimeError):
    pass

class LeaseRequired(RuntimeError):
    pass


def database_wall_clock():
    return func.clock_timestamp()


def ownership_predicate(job_id, token):
    return (
        (ImportJob.id == job_id)
        & (ImportJob.status == RUNNING)
        & ImportJob.lease_token.is_not(None)
        & (ImportJob.lease_token == token)
        & ImportJob.lease_expires_at.is_not(None)
        & (ImportJob.lease_expires_at > database_wall_clock())
    )


def _overlay_attachment_truth(session, report, job_id, attempt_number) -> None:
    """Use committed attempt evidence instead of historical batch deltas."""
    observations = session.scalars(
        select(AttachmentObservation)
        .where(AttachmentObservation.import_job_id == job_id)
        .order_by(AttachmentObservation.attachment_id, AttachmentObservation.attempt_number)
    )
    chosen_by_attachment = {}
    for observation in observations:
        chosen_by_attachment.setdefault(observation.attachment_id, observation.availability)
        if observation.attempt_number == attempt_number:
            chosen_by_attachment[observation.attachment_id] = observation.availability
    if not chosen_by_attachment:
        return
    rows = Counter(chosen_by_attachment.values()).items()
    counts = {key: 0 for key in ("available", "missing", "referenced_only", "failed")}
    for availability, count in rows:
        counts[availability] = int(count)
    report["attachments"] = counts


def aggregate_durable_job_state(
    session: Session,
    job_id,
    attempt_number: int,
    *,
    terminal_report: dict | None = None,
) -> DurableJobState:
    """Build the one authoritative summary from ordered committed evidence."""
    batches = tuple(
        session.scalars(
            select(ImportBatchCommit)
            .where(ImportBatchCommit.import_job_id == job_id)
            .order_by(ImportBatchCommit.sequence_no, ImportBatchCommit.id)
        ).all()
    )
    numeric_sections = ("conversations", "messages", "revisions")
    list_sections = ("tree_anomalies", "unknown", "errors")
    if batches:
        identities = {(batch.parser_name, batch.contract_version) for batch in batches}
        if len(identities) != 1:
            raise DurableLedgerInconsistent("conflicting_durable_parser_identity")
        parser_name, parser_version = next(iter(identities))
        first_delta = _validate_durable_report_delta(batches[0])
        parser = first_delta["parser"]
        report = empty_report(
            parser_name,
            parser_version,
            parser.get("detection", "matched"),
        )
    else:
        supplied_parser = (terminal_report or {}).get("parser", {})
        report = empty_report(
            supplied_parser.get("name", ""),
            supplied_parser.get("version", ""),
            supplied_parser.get("detection", ""),
        )

    for batch in batches:
        delta = _validate_durable_report_delta(batch)
        expected_counts = _counts_from_report_delta(delta)
        raw_counts = batch.counts_delta
        if not isinstance(raw_counts, dict):
            raise DurableLedgerInconsistent("invalid_durable_counts_delta")
        actual_counts = {}
        for key in expected_counts:
            value = raw_counts.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise DurableLedgerInconsistent("invalid_durable_counts_delta")
            actual_counts[key] = value
        if actual_counts != expected_counts:
            raise DurableLedgerInconsistent("inconsistent_durable_batch_counts")
        for section in numeric_sections:
            for key in report[section]:
                report[section][key] += int(delta.get(section, {}).get(key, 0))
        # Attachment deltas are deliberately excluded: retry history is not truth.
        for section in list_sections:
            report[section].extend(copy.deepcopy(delta.get(section, [])))

    supplied = terminal_report or {}
    if not isinstance(supplied, dict):
        supplied = {}
    if not batches:
        for section in (*numeric_sections, "attachments"):
            values = supplied.get(section)
            if not isinstance(values, dict):
                continue
            for key in report[section]:
                value = values.get(key, 0)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise DurableLedgerInconsistent("invalid_terminal_report")
                report[section][key] = value
    # Preserve terminal diagnostics and non-summary metadata without allowing a
    # live/rolled-back report to overwrite committed ledger truth.
    overlay_sections = ("errors",) if batches else list_sections
    for section in overlay_sections:
        for item in supplied.get(section, []):
            if item not in report[section]:
                report[section].append(copy.deepcopy(item))
    if batches:
        for item in supplied.get("unknown", []):
            sanitized = _sanitize_terminal_unknown(item)
            if sanitized is not None and sanitized not in report["unknown"]:
                report["unknown"].append(sanitized)
    protected = {"parser", *numeric_sections, "attachments", *list_sections,
                 "items", "imported_messages", "imported_conversations",
                 "durable_batches", "durable_counts"}
    for key, value in supplied.items():
        if key not in protected:
            report[key] = copy.deepcopy(value)

    counts = (
        {
            key: sum(int(batch.counts_delta[key]) for batch in batches)
            for key in ("discovered_count", "imported_count", "skipped_count", "failed_count")
        }
        if batches
        else _counts_from_report_delta(report)
    )
    _overlay_attachment_truth(session, report, job_id, attempt_number)
    report["items"] = {
        "conversations": sum(report["conversations"].values()),
        "messages": sum(report["messages"].values()),
    }
    report["imported_messages"] = report["messages"]["new"]
    report["imported_conversations"] = report["conversations"]["new"]
    if batches:
        report["durable_batches"] = len(batches)
        report["durable_counts"] = counts
    return DurableJobState(batches=batches, counts=counts, report=report)


def _counts_from_report_delta(delta: dict) -> dict[str, int]:
    messages = delta.get("messages", {})
    normalized = {}
    for key in ("new", "existing", "changed", "skipped", "failed"):
        value = messages.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DurableLedgerInconsistent("invalid_durable_report_delta")
        normalized[key] = value
    return {
        "discovered_count": sum(normalized.values()),
        "imported_count": normalized["new"],
        "skipped_count": normalized["existing"] + normalized["skipped"],
        "failed_count": normalized["failed"],
    }


def _validate_durable_report_delta(batch: ImportBatchCommit) -> dict:
    delta = batch.report_delta
    if not isinstance(delta, dict):
        raise DurableLedgerInconsistent("invalid_durable_report_delta")
    parser = delta.get("parser")
    if not isinstance(parser, dict) or (
        parser.get("name"), parser.get("version")
    ) != (batch.parser_name, batch.contract_version):
        raise DurableLedgerInconsistent("inconsistent_durable_parser_identity")
    if not isinstance(parser.get("detection", "matched"), str):
        raise DurableLedgerInconsistent("invalid_durable_report_delta")
    section_keys = {
        "conversations": ("new", "existing", "changed", "skipped", "failed"),
        "messages": ("new", "existing", "changed", "skipped", "failed"),
        "revisions": ("new", "existing"),
    }
    for section, keys in section_keys.items():
        values = delta.get(section)
        if not isinstance(values, dict):
            raise DurableLedgerInconsistent("invalid_durable_report_delta")
        for key in keys:
            value = values.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise DurableLedgerInconsistent("invalid_durable_report_delta")
    for section in ("tree_anomalies", "unknown", "errors"):
        if not isinstance(delta.get(section), list):
            raise DurableLedgerInconsistent("invalid_durable_report_delta")
    return delta


def _sanitize_terminal_unknown(item) -> dict | None:
    """Keep safe diagnostic text, never terminal summary or arbitrary metadata."""
    if not isinstance(item, dict) or not isinstance(item.get("type"), str):
        return None
    sanitized = {"type": item["type"]}
    for key in ("detail", "raw_pointer", "file", "field"):
        value = item.get(key)
        if isinstance(value, str):
            sanitized[key] = value
    severity = item.get("severity")
    if severity in {"info", "warning", "error"}:
        sanitized["severity"] = severity
    return sanitized


def apply_durable_counts(job, snapshot: DurableJobState):
    job.discovered_count = snapshot.counts["discovered_count"]
    job.imported_count = snapshot.counts["imported_count"]
    job.skipped_count = snapshot.counts["skipped_count"]
    job.failed_count = snapshot.counts["failed_count"]
    job.items_discovered = snapshot.counts["discovered_count"]
    job.items_imported = snapshot.counts["imported_count"]
    job.items_skipped = snapshot.counts["skipped_count"]
    job.items_failed = snapshot.counts["failed_count"]
