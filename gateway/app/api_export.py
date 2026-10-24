"""GET-only export of committed evidence.

These routes never update or delete evidence. Conversation export uses the
evidence-read credential. The full archive export, which includes raw object
bytes, uses the operator API credential.
"""

from __future__ import annotations

import logging
from pathlib import Path
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .auth import require_evidence_read_token, require_token
from .config import get_settings
from .db import get_db
from .export.bundle import FORMAT
from .export.service import ConversationNotFound, ExportBundle, ExportIntegrityError, ExportService


logger = logging.getLogger(__name__)
_CHUNK = 1024 * 1024

conversation_router = APIRouter(
    prefix="/api/v1/export",
    dependencies=[Depends(require_evidence_read_token)],
)
archive_router = APIRouter(
    prefix="/api/v1/export",
    dependencies=[Depends(require_token)],
)


def _iter_bundle(bundle: ExportBundle):
    try:
        with bundle.path.open("rb") as handle:
            while chunk := handle.read(_CHUNK):
                yield chunk
    finally:
        bundle.unlink()


def _response(bundle: ExportBundle, filename: str) -> StreamingResponse:
    return StreamingResponse(
        _iter_bundle(bundle),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Qingyu-Export-Format": FORMAT,
            "X-Qingyu-Manifest-SHA256": bundle.manifest_sha256,
            "Cache-Control": "private, no-store",
        },
    )


def _service(db: Session) -> ExportService:
    return ExportService(db, Path(get_settings().archive_root))


@conversation_router.get("/conversations/{conversation_id}")
def export_conversation(
    conversation_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    bundle = None
    try:
        bundle = _service(db).export_conversation(conversation_id)
        return _response(bundle, f"qingyu-conversation-{conversation_id}.zip")
    except ConversationNotFound as exc:
        if bundle is not None:
            bundle.unlink()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "conversation_not_found"},
        ) from exc
    except ExportIntegrityError as exc:
        if bundle is not None:
            bundle.unlink()
        logger.error("export integrity failure", extra={"error_code": "export_integrity_failure"})
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "export_integrity_failure"},
        ) from exc
    except Exception:
        if bundle is not None:
            bundle.unlink()
        raise


@archive_router.get("/archive")
def export_archive(db: Session = Depends(get_db)) -> StreamingResponse:
    bundle = None
    try:
        bundle = _service(db).export_archive()
        return _response(bundle, "qingyu-archive.zip")
    except ExportIntegrityError as exc:
        if bundle is not None:
            bundle.unlink()
        logger.error("export integrity failure", extra={"error_code": "export_integrity_failure"})
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "export_integrity_failure"},
        ) from exc
    except Exception:
        if bundle is not None:
            bundle.unlink()
        raise
