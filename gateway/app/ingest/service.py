"""Register one native write on the archive and import-job path.

The ledger insert, archive rows, and ImportJob commit together. The manifest
is published after that commit, which is the same order as ZIP import.
Nothing here claims or runs the job.
"""
from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..archive.service import ArchiveService
from ..importers.native import PARSER_NAME, PARSER_VERSION
from ..importers.staging import create_prejob_generation
from ..importers.status import PENDING
from ..importers.worker import ImportWorker
from ..models import (
    ConversationObservation,
    ImportBatchCommit,
    ImportJob,
    IngestRequest,
    MessageObservation,
    SourceAccount,
)

_RETRY_AFTER_SECONDS = 1


@dataclass(frozen=True)
class IngestResult:
    status_code: int
    body: dict


def submit_native_document(
    db: Session,
    *,
    archive_root: Path,
    client_id: str,
    client_request_id: str,
    payload_hash: str,
    document: bytes,
    kind: str,
    source_platform: str,
    wait_seconds: float,
) -> IngestResult:
    """Archive ``document`` once for this client request id.

    A second call with the same id and the same hash returns the original
    outcome. A second call with a different hash is a conflict and does not
    create another job.
    """
    existing = _load(db, client_id, client_request_id)
    if existing is not None:
        db.rollback()
        return _replay(db, existing, payload_hash, wait_seconds)

    db.rollback()
    generation = create_prejob_generation(archive_root, "uploads")
    temp = generation.payload_path()
    try:
        temp.write_bytes(document)
        archive = ArchiveService(archive_root, db)
        prepared = archive.prepare(temp, "native.json", source_platform, "application/json")
        if not secrets.compare_digest(prepared.sha256, payload_hash):
            raise RuntimeError("archived bytes do not match payload_hash")
        try:
            inserted_id = _insert_ledger(
                db,
                client_id=client_id,
                client_request_id=client_request_id,
                payload_hash=payload_hash,
                kind=kind,
            )
            if inserted_id is None:
                db.rollback()
                raced = _load(db, client_id, client_request_id)
                if raced is None:
                    db.rollback()
                    raise RuntimeError("ingest request conflict did not leave a row")
                return _replay(db, raced, payload_hash, wait_seconds)
            row = db.get(IngestRequest, inserted_id)
            if row is None:
                raise RuntimeError("ingest request insert was not visible")
            account = _source_account(db, source_platform, client_id)
            if account is None:
                db.rollback()
                return IngestResult(409, {
                    "code": "source_account_conflict",
                    "client_request_id": client_request_id,
                })
            pending = archive.persist_prepared(prepared)
            job = ImportJob(
                source_account_id=account.id,
                archive_object_id=pending.object_id,
                parser_name=PARSER_NAME,
                parser_version=PARSER_VERSION,
                status=PENDING,
                report_json={},
            )
            db.add(job)
            db.flush()
            pending.observation.import_job_id = job.id
            row.archive_object_id = pending.object_id
            row.import_job_id = job.id
            row.updated_at = datetime.now(UTC)
            db.flush()
            db.commit()
        except Exception:
            db.rollback()
            raise
        # Commit is the point the job becomes visible. The manifest file is
        # published after that, and workers skip the job until the file exists.
        archive.finalize(pending)
    finally:
        generation.close(remove=True)
    return _await(db, client_id, client_request_id, wait_seconds)


def read_ingest_request(db: Session, *, client_id: str, client_request_id: str) -> IngestResult | None:
    row = _load(db, client_id, client_request_id)
    if row is None:
        return None
    row = reconcile(db, row)
    return IngestResult(200, _public(row, include_result=True))


def reconcile(db: Session, row: IngestRequest) -> IngestRequest:
    """Move archived to completed or failed when the ImportJob has finished.

    Conditional on status='archived', so a repeated read does not rewrite a
    stored result.
    """
    if row.status != "archived" or row.import_job_id is None:
        return row
    db.expire_all()
    row = db.get(IngestRequest, row.id)
    if row is None or row.status != "archived" or row.import_job_id is None:
        return row
    job = db.scalar(
        select(ImportJob)
        .where(ImportJob.id == row.import_job_id)
        .execution_options(populate_existing=True)
    )
    if job is None:
        return row
    now = datetime.now(UTC)
    if job.status == "completed" and job.result_code == "imported":
        values = {
            "status": "completed",
            "result_json": _result_json(db, job),
            "error_code": None,
            "updated_at": now,
        }
    elif job.status in {"failed", "partial", "cancelled"}:
        values = {
            "status": "failed",
            "error_code": job.error_code or job.result_code or job.status,
            "updated_at": now,
        }
    else:
        return row
    db.execute(
        update(IngestRequest)
        .where(IngestRequest.id == row.id, IngestRequest.status == "archived")
        .values(**values)
    )
    db.commit()
    db.expire_all()
    refreshed = db.get(IngestRequest, row.id)
    return refreshed if refreshed is not None else row


def _replay(db: Session, row: IngestRequest, payload_hash: str, wait_seconds: float) -> IngestResult:
    if not _same_hash(row.payload_hash, payload_hash):
        return IngestResult(409, {
            "code": "idempotency_key_reused",
            "status": _public_status(row.status),
            "ingest_request_id": str(row.id),
            "client_request_id": row.client_request_id,
        })
    row = reconcile(db, row)
    if row.status == "failed":
        retried = _retry_failed(db, row)
        if retried:
            return _processing(retried)
        return IngestResult(409, {
            "code": "ingest_not_retryable",
            "status": "failed",
            "error_code": row.error_code,
            "ingest_request_id": str(row.id),
            "client_request_id": row.client_request_id,
        })
    if row.status == "archived":
        return _await(db, row.client_id, row.client_request_id, wait_seconds)
    if row.status == "completed":
        return IngestResult(200, _public(row, include_result=True))
    return IngestResult(409, {
        "code": "ingest_not_retryable",
        "status": _public_status(row.status),
        "ingest_request_id": str(row.id),
        "client_request_id": row.client_request_id,
    })


def _retry_failed(db: Session, row: IngestRequest) -> IngestRequest | None:
    if row.import_job_id is None:
        return None
    accepted = ImportWorker(db).retry(
        row.import_job_id,
        expected_parser_name=PARSER_NAME,
        expected_parser_version=PARSER_VERSION,
    )
    if not accepted:
        return None
    now = datetime.now(UTC)
    updated = db.execute(
        update(IngestRequest)
        .where(IngestRequest.id == row.id, IngestRequest.status == "failed")
        .values(status="archived", error_code=None, result_json=None, updated_at=now)
    )
    db.commit()
    if updated.rowcount != 1:
        db.rollback()
        return None
    db.expire_all()
    return db.get(IngestRequest, row.id)


def _await(db: Session, client_id: str, client_request_id: str, wait_seconds: float) -> IngestResult:
    """Poll the ledger until the job finishes or the wait elapses.

    The API process does not claim the job. A wait of zero returns 202 as
    soon as the row is still archived.
    """
    deadline = time.monotonic() + max(wait_seconds, 0.0)
    while True:
        db.rollback()
        row = _load(db, client_id, client_request_id)
        if row is None:
            raise RuntimeError("ingest request disappeared before the response")
        row = reconcile(db, row)
        if row.status == "completed":
            return IngestResult(200, _public(row, include_result=True))
        if row.status == "failed":
            return IngestResult(422, {
                "code": "ingest_failed",
                "status": "failed",
                "error_code": row.error_code,
                "ingest_request_id": str(row.id),
                "client_request_id": row.client_request_id,
            })
        if time.monotonic() >= deadline:
            return _processing(row)
        time.sleep(0.05)


def _processing(row: IngestRequest) -> IngestResult:
    body = _public(row, include_result=False)
    body["retry_after_seconds"] = _RETRY_AFTER_SECONDS
    return IngestResult(202, body)


def _public(row: IngestRequest, *, include_result: bool) -> dict:
    body = {
        "ingest_request_id": str(row.id),
        "client_request_id": row.client_request_id,
        "status": _public_status(row.status),
        "import_job_id": str(row.import_job_id) if row.import_job_id else None,
        "archive_object_id": str(row.archive_object_id) if row.archive_object_id else None,
        "status_url": f"/api/v1/ingest/requests/{row.client_request_id}",
    }
    if include_result and row.status == "completed":
        body["result"] = row.result_json or {}
    if row.status == "failed" and row.error_code:
        body["error_code"] = row.error_code
    return body


def _public_status(status: str) -> str:
    if status == "archived":
        return "processing"
    return status


def _same_hash(stored: str, supplied: str) -> bool:
    if len(stored) != len(supplied):
        return False
    return secrets.compare_digest(stored, supplied)


def _load(db: Session, client_id: str, client_request_id: str) -> IngestRequest | None:
    return db.scalar(
        select(IngestRequest)
        .where(
            IngestRequest.client_id == client_id,
            IngestRequest.client_request_id == client_request_id,
        )
        .execution_options(populate_existing=True)
    )


def _insert_ledger(db: Session, *, client_id: str, client_request_id: str, payload_hash: str, kind: str):
    now = datetime.now(UTC)
    statement = (
        pg_insert(IngestRequest)
        .values(
            id=uuid.uuid4(),
            client_id=client_id,
            client_request_id=client_request_id,
            payload_hash=payload_hash,
            kind=kind,
            status="archived",
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_ingest_requests_client_request")
        .returning(IngestRequest.id)
    )
    return db.execute(statement).scalar_one_or_none()


def _source_account(db: Session, platform: str, client_id: str) -> SourceAccount | None:
    account = db.scalar(
        select(SourceAccount).where(
            SourceAccount.platform == platform,
            SourceAccount.account_label == client_id,
        )
    )
    if account is None:
        account = SourceAccount(
            platform=platform,
            account_label=client_id,
            metadata_json={"provenance": "client_declared"},
        )
        db.add(account)
        db.flush()
        return account
    metadata = account.metadata_json or {}
    if metadata.get("provenance") != "client_declared":
        return None
    return account


def _result_json(db: Session, job: ImportJob) -> dict:
    batches = list(db.scalars(
        select(ImportBatchCommit)
        .where(ImportBatchCommit.import_job_id == job.id)
        .order_by(ImportBatchCommit.sequence_no)
    ))
    conversation_id = str(batches[0].conversation_id) if batches else None
    observation_ids = []
    for batch in batches:
        observation_id = db.scalar(
            select(ConversationObservation.id).where(
                ConversationObservation.import_batch_commit_id == batch.id
            )
        )
        if observation_id is not None:
            observation_ids.append(str(observation_id))
    observations = db.scalars(
        select(MessageObservation)
        .where(MessageObservation.import_job_id == job.id)
        .order_by(MessageObservation.observation_order, MessageObservation.id)
    )
    messages = [
        {
            "client_message_id": observation.external_node_id,
            "message_id": str(observation.message_id),
            "message_revision_id": str(observation.message_revision_id),
            "message_observation_id": str(observation.id),
        }
        for observation in observations
    ]
    return {
        "conversation_id": conversation_id,
        "conversation_observation_ids": observation_ids,
        "messages": messages,
    }
