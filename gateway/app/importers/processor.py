import hashlib
import logging
import zipfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ..archive.service import ArchiveService
from ..errors import safe_log_exception
from ..models import ArchiveObject, ImportBatchCommit, ImportJob, SourceAccount
from .attachments import prepare_attachment_work
from .cleanup import cleanup_terminal_attempt
from .generic import ParserRegistry, ParserValidationError, load_json_document
from .retry_policy import (
    ARCHIVE_INTEGRITY_FAILURE,
    resolve_durable_parser_contract,
)
from .service import BatchReplayIdentityError, ImportService
from .staging import enqueue_staging_candidate
from .worker import ImportWorker, LeaseLost
from .zip_staging import UnsupportedZipError, ZipLimits, safe_extract

logger = logging.getLogger(__name__)
_DEFAULT_ZIP_LIMITS = ZipLimits()

class ImportProcessor:
    def __init__(self, db, archive_root: Path, limits: ZipLimits = _DEFAULT_ZIP_LIMITS, registry=None):
        self.db, self.root, self.limits = db, archive_root, limits
        self.registry = registry or ParserRegistry()
    def process(self, job, token, health_check=None) -> dict:
        health_check = health_check or (lambda: None)
        job_id = job.id
        worker=ImportWorker(self.db); health_check(); worker.check_ownership(job, token)
        attempt = None
        keep=False
        cleanup_completed = False
        try:
            obj=self.db.get(ArchiveObject, job.archive_object_id)
            if obj is None: raise ValueError('archive_object_missing')
            if obj.integrity_status != 'verified':
                raise NonRetryablePreflightError(ARCHIVE_INTEGRITY_FAILURE)
            committed_identities = self.db.execute(
                select(ImportBatchCommit.parser_name, ImportBatchCommit.contract_version)
                .where(ImportBatchCommit.import_job_id == job_id)
                .distinct()
            ).all()
            persisted_job = self.db.scalar(
                select(ImportJob)
                .where(ImportJob.id == job_id)
                .execution_options(populate_existing=True)
            )
            if persisted_job is None:
                raise ValueError('import_job_missing')
            pinned_parser, parser_block = resolve_durable_parser_contract(
                persisted_job,
                (
                    (parser_name, contract_version)
                    for parser_name, contract_version in committed_identities
                ),
                registry=self.registry,
            )
            if parser_block is not None:
                raise NonRetryablePreflightError(parser_block)
            source=self.root/obj.relative_path
            attempt=Path(worker.attempt_staging_path(self.root, job))
            jobs_root = (self.root / "staging" / "jobs").absolute()
            attempt = attempt.absolute()
            if attempt.is_relative_to(jobs_root):
                enqueue_staging_candidate(
                    jobs_root,
                    attempt.relative_to(jobs_root).as_posix(),
                )
            attempt.mkdir(parents=True, exist_ok=False)
            verified_source = attempt / '_verified_input.zip'
            actual_size, actual_sha = copy_and_verify_archive(source, verified_source)
            health_check()
            if actual_size != obj.size_bytes or actual_sha != obj.sha256:
                raise ArchiveConsumptionIntegrityError(
                    archive_object_id=obj.id,
                    expected_sha256=obj.sha256,
                    actual_sha256=actual_sha,
                    expected_size=obj.size_bytes,
                    actual_size=actual_size,
                )
            if not zipfile.is_zipfile(verified_source): raise UnsupportedStructure('unsupported_structure')
            extract_root = attempt / 'extracted'
            scan=safe_extract(verified_source, extract_root, self.limits)
            health_check()
            files=list(extract_root.rglob('*')); candidates=[p for p in files if p.is_file() and p.suffix.lower()=='.json' and p.name in {'fixture.json','conversations.json'}]
            unknown_count=sum(1 for p in files if p.is_file() and p not in candidates)
            if not candidates: raise UnsupportedStructure('unsupported_structure')
            if len(candidates) != 1: raise UnsupportedStructure('ambiguous_structure')
            source_member = candidates[0].relative_to(extract_root).as_posix()
            payload=load_json_document(candidates[0].read_text(encoding='utf-8'), source_member)
            if pinned_parser is None:
                detection,parser=self.registry.detect(payload, source_member)
                if detection != 'matched': raise UnsupportedStructure(detection)
            else:
                parser = pinned_parser
            unknown_fields = parser.validate(payload, source_member)
            health_check()
            parsed_conversations = tuple(parser.parse(payload))
            account = self.db.get(SourceAccount, job.source_account_id)
            if account is None:
                raise ValueError('invalid_source_account')
            attachment_archive = ArchiveService(self.root, self.db)
            attachment_work = prepare_attachment_work(
                parsed_conversations,
                extract_root,
                attachment_archive,
                source_platform=account.platform,
                source_member=source_member,
                health_check=health_check,
            )
            health_check()
            outcome=ImportService(self.db).import_fixture(payload, job.archive_object_id, job=job, lease_token=token, source_member=source_member, parser=parser, unknown_entries=unknown_fields, health_check=health_check, parsed_conversations=parsed_conversations, attachment_work=attachment_work, attachment_archive=attachment_archive)
            result=dict(outcome.report)
            result['scan'] = scan
            result['unknown'] = [
                *result.get('unknown', []),
                *([{'type':'unparsed_archive_members','count':unknown_count,'severity':'info'}] if unknown_count else []),
            ]
            result['imported_messages']=result.get('messages',{}).get('new',0); result['imported_conversations']=result.get('conversations',{}).get('new',0)
            health_check()
            if result.get('attachments', {}).get('failed', 0):
                worker.mark_failed_or_partial(job_id, token, 'attachment_processing_failed', result, 'attachment_processing_failed')
            elif not worker.mark_completed(job_id, token, result, 'imported', outcome.counts):
                raise LeaseLost('ownership_lost')
            else:
                cleanup_completed = True
            return result
        except LeaseLost:
            keep=True; self.db.rollback(); raise
        except NonRetryablePreflightError as exc:
            keep=True
            self.db.rollback()
            worker.mark_failed_or_partial(
                job_id,
                token,
                exc.code,
                {'errors':[{'type':exc.code,'detail':'retry prerequisites are not available','severity':'error'}]},
                exc.code,
            )
            raise
        except ArchiveConsumptionIntegrityError as exc:
            keep=True
            self.db.rollback()
            worker.mark_archive_integrity_failure(
                job_id=job_id,
                token=token,
                archive_object_id=exc.archive_object_id,
                archive_root=self.root,
                integrity_error='archive_integrity_failure: digest_or_size_mismatch',
                report={'errors':[{'type':'archive_integrity_failure','detail':'archive bytes failed integrity verification','severity':'error'}]},
            )
            raise
        except ParserValidationError as exc:
            keep=True; self.db.rollback()
            report={'errors':[dict(item, severity='error') for item in exc.errors]}
            worker.mark_failed_or_partial(job_id, token, 'parser_validation_error', report, 'parser_validation_error')
            raise
        except UnsupportedZipError as exc:
            keep=True
            self.db.rollback()
            report={'errors':[{'type':exc.code,'detail':'encrypted ZIP members are unsupported','severity':'error'}]}
            worker.mark_failed_or_partial(job_id, token, exc.code, report, exc.code)
            raise
        except UnsupportedStructure as exc:
            keep=True
            self.db.rollback()
            worker.mark_failed_or_partial(job_id, token, exc.code, {'unknown':[{'type':exc.code,'detail':'no registered parser matched','severity':'error'}]}, exc.code)
            raise
        except BatchReplayIdentityError:
            keep=True
            self.db.rollback()
            worker.mark_failed_or_partial(
                job_id,
                token,
                'batch_replay_identity_mismatch',
                {'errors':[{'type':'batch_replay_identity_mismatch','detail':'durable batch identity does not match replay input','severity':'error'}]},
                'batch_replay_identity_mismatch',
            )
            raise
        except SQLAlchemyError:
            keep=True
            self.db.rollback()
            raise
        except Exception:
            keep=True
            self.db.rollback()
            worker.mark_failed_or_partial(job_id, token, 'processor_error', {'errors':[{'type':'processor_error','detail':'processing failed','severity':'error'}]}, 'processor_error')
            raise
        finally:
            if cleanup_completed and not keep and attempt is not None:
                try:
                    cleanup = cleanup_terminal_attempt(
                        self.root,
                        self.db,
                        job_id,
                        job.attempt_count,
                        token,
                    )
                    if cleanup.outcome not in {"deleted", "missing"}:
                        logger.warning(
                            "terminal attempt staging cleanup incomplete",
                            extra={"cleanup_outcome": cleanup.outcome},
                        )
                except Exception as exc:  # noqa: BLE001
                    safe_log_exception(
                        logger,
                        logging.ERROR,
                        "terminal attempt staging cleanup failed",
                        error_code="terminal_staging_cleanup_failed",
                        operation="cleanup_terminal_staging",
                        exception=exc,
                    )

class UnsupportedStructure(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class NonRetryablePreflightError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

class ArchiveConsumptionIntegrityError(IOError):
    def __init__(self, *, archive_object_id, expected_sha256: str, actual_sha256: str, expected_size: int, actual_size: int):
        self.archive_object_id = archive_object_id
        self.expected_sha256 = expected_sha256
        self.actual_sha256 = actual_sha256
        self.expected_size = expected_size
        self.actual_size = actual_size
        super().__init__(
            f'archive_integrity_failure: expected={expected_sha256}/{expected_size} '
            f'actual={actual_sha256}/{actual_size}'
        )


def copy_and_verify_archive(source: Path, destination: Path) -> tuple[int, str]:
    digest = hashlib.sha256(); size = 0
    with source.open('rb') as src, destination.open('xb') as dst:
        for chunk in iter(lambda: src.read(1024 * 1024), b''):
            dst.write(chunk); digest.update(chunk); size += len(chunk)
    return size, digest.hexdigest()
