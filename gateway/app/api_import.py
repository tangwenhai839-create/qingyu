import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .archive.service import ArchiveService
from .auth import require_token
from .config import get_settings
from .db import get_db
from .importers.generic import ParserRegistry
from .importers.retry_policy import resolve_durable_parser_contract
from .importers.staging import create_prejob_generation
from .importers.status import PENDING
from .importers.worker import ImportWorker
from .models import ImportBatchCommit, ImportJob, SourceAccount

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_token)])
logger = logging.getLogger(__name__)

UPLOAD_CHUNK_BYTES = 1024 * 1024
PENDING_PARSER_NAME = "pending-detection"
PENDING_PARSER_VERSION = "1"


def _write_upload_with_limit(file: UploadFile, destination: Path, max_bytes: int) -> None:
    written = 0
    with destination.open("wb") as out:
        while chunk := file.file.read(min(UPLOAD_CHUNK_BYTES, max_bytes - written + 1)):
            written += len(chunk)
            if written > max_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail=f"上传文件超过 {max_bytes} 字节限制",
                )
            out.write(chunk)


@router.post("/import/chatgpt", status_code=status.HTTP_202_ACCEPTED)
def create_import(
    file: UploadFile = File(...),
    source_platform: str = Form("chatgpt"),
    db: Session = Depends(get_db),
) -> dict:
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(415, "仅支持 ZIP 导入")
    settings = get_settings()
    root = Path(settings.archive_root)
    generation = create_prejob_generation(root, "uploads")
    temp = generation.payload_path()
    try:
        _write_upload_with_limit(file, temp, settings.import_upload_max_bytes)
        archive = ArchiveService(root, db)
        prepared = archive.prepare(temp, file.filename, source_platform, file.content_type)
        try:
            pending_archive = archive.persist_prepared(prepared)
            account = db.scalar(
                select(SourceAccount).where(
                    SourceAccount.platform == source_platform,
                    SourceAccount.account_label == "default",
                )
            )
            if account is None:
                account = SourceAccount(
                    platform=source_platform,
                    account_label="default",
                    metadata_json={},
                )
                db.add(account)
                db.flush()
            job = ImportJob(
                source_account_id=account.id,
                archive_object_id=pending_archive.object_id,
                parser_name=PENDING_PARSER_NAME,
                parser_version=PENDING_PARSER_VERSION,
                status=PENDING,
                report_json={},
            )
            db.add(job)
            db.flush()
            job_id = job.id
            pending_archive.observation.import_job_id = job.id
            db.flush()
            db.commit()
        except Exception:
            db.rollback()
            raise
        # The job row is visible as soon as this commit lands. finalize()
        # publishes manifests/<sha>.json afterwards. Workers skip a pending
        # job until that regular file exists, so the gap is not claimable.
        archive.finalize(pending_archive)
        return {"import_job_id": str(job_id)}
    finally:
        cleanup = generation.close(remove=True)
        if cleanup is not None and cleanup.status not in {"deleted", "missing"}:
            logger.warning(
                "import upload staging cleanup incomplete",
                extra={"cleanup_outcome": cleanup.status},
            )

@router.get("/import-jobs/{job_id}")
def get_import_job(job_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    job = db.get(ImportJob, job_id)
    if job is None: raise HTTPException(404, "Import Job not found")
    return {"import_job_id": str(job.id), "status": job.status, "result_code": job.result_code, "error_code": job.error_code, "progress": job.checkpoint, "counts": {"discovered": job.discovered_count, "imported": job.imported_count, "skipped": job.skipped_count, "failed": job.failed_count}, "errors": job.error_summary, "report": job.report_json}


@router.post("/import-jobs/{job_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_import_job(job_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    job = db.get(ImportJob, job_id)
    if job is None:
        raise HTTPException(404, "Import Job not found")
    identities = db.execute(
        select(ImportBatchCommit.parser_name, ImportBatchCommit.contract_version)
        .where(ImportBatchCommit.import_job_id == job_id)
        .distinct()
    ).all()
    registry = ParserRegistry()
    pinned_parser, block_reason = resolve_durable_parser_contract(job, identities, registry=registry)
    if block_reason is not None:
        db.rollback()
        raise HTTPException(409, block_reason)
    expected_name = pinned_parser.name if pinned_parser is not None else job.parser_name
    expected_version = pinned_parser.version if pinned_parser is not None else job.parser_version
    db.rollback()
    if not ImportWorker(db).retry(
        job_id,
        expected_parser_name=expected_name,
        expected_parser_version=expected_version,
    ):
        raise HTTPException(409, "Import Job is not retryable")
    return {"import_job_id": str(job_id), "status": PENDING}

