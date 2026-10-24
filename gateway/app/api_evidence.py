import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from .auth import require_evidence_read_token
from .config import Settings, get_settings
from .db import get_db
from .evidence.cursor import InvalidCursor, decode_cursor, encode_cursor
from .evidence.query import EvidenceIntegrityError, EvidenceQueryService
from .evidence.schemas import AttachmentObservationPage, NodePage, ObservationPage

router = APIRouter(
    prefix="/api/v1/evidence",
    dependencies=[Depends(require_evidence_read_token)],
)
logger = logging.getLogger(__name__)


def _private_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


def _cursor_secret(settings: Settings) -> str:
    return settings.evidence_cursor_signing_key


def _invalid_cursor() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"code": "evidence_cursor_invalid"},
    )


def _decode_observation_cursor(
    cursor: str,
    *,
    settings: Settings,
    source_account_id: uuid.UUID | None,
    conversation_id: uuid.UUID | None,
):
    expected_filters = {
        "source_account_id": str(source_account_id) if source_account_id else None,
        "conversation_id": str(conversation_id) if conversation_id else None,
    }
    try:
        payload = decode_cursor(cursor, _cursor_secret(settings))
        if payload.get("kind") != "conversation-observations":
            raise InvalidCursor("wrong_kind")
        if payload.get("filters") != expected_filters:
            raise InvalidCursor("scope_mismatch")
        upper = payload["upper"]
        after = payload["after"]
        return (
            (datetime.fromisoformat(upper["committed_at"]), uuid.UUID(upper["id"])),
            (datetime.fromisoformat(after["committed_at"]), uuid.UUID(after["id"])),
        )
    except (InvalidCursor, KeyError, TypeError, ValueError) as exc:
        raise _invalid_cursor() from exc


def _decode_node_cursor(
    cursor: str,
    *,
    settings: Settings,
    observation_id: uuid.UUID,
):
    try:
        payload = decode_cursor(cursor, _cursor_secret(settings))
        if payload.get("kind") != "observation-nodes":
            raise InvalidCursor("wrong_kind")
        if payload.get("observation_id") != str(observation_id):
            raise InvalidCursor("scope_mismatch")
        after = payload["after"]
        return (int(after["observation_order"]), uuid.UUID(after["id"]))
    except (InvalidCursor, KeyError, TypeError, ValueError) as exc:
        raise _invalid_cursor() from exc


def _decode_attachment_cursor(
    cursor: str,
    *,
    settings: Settings,
    observation_id: uuid.UUID,
    attachment_id: uuid.UUID,
):
    try:
        payload = decode_cursor(cursor, _cursor_secret(settings))
        if payload.get("kind") != "attachment-observations":
            raise InvalidCursor("wrong_kind")
        if payload.get("observation_id") != str(observation_id):
            raise InvalidCursor("scope_mismatch")
        if payload.get("attachment_id") != str(attachment_id):
            raise InvalidCursor("scope_mismatch")
        upper = payload["upper"]
        after = payload["after"]
        return (
            (
                int(upper["attempt_number"]),
                datetime.fromisoformat(upper["observed_at"]),
                uuid.UUID(upper["id"]),
            ),
            (
                int(after["attempt_number"]),
                datetime.fromisoformat(after["observed_at"]),
                uuid.UUID(after["id"]),
            ),
        )
    except (InvalidCursor, KeyError, TypeError, ValueError) as exc:
        raise _invalid_cursor() from exc


@router.get("/conversation-observations", response_model=ObservationPage)
def list_conversation_observations(
    response: Response,
    source_account_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ObservationPage:
    _private_no_store(response)
    upper_bound = None
    after = None
    if cursor is not None:
        upper_bound, after = _decode_observation_cursor(
            cursor,
            settings=settings,
            source_account_id=source_account_id,
            conversation_id=conversation_id,
        )
    page = EvidenceQueryService(db).list_conversation_observations(
        limit=limit,
        source_account_id=source_account_id,
        conversation_id=conversation_id,
        upper_bound=upper_bound,
        after=after,
    )
    next_cursor = None
    if page.has_more and page.items and page.upper_bound is not None:
        last = page.items[-1]
        next_cursor = encode_cursor(
            {
                "kind": "conversation-observations",
                "filters": {
                    "source_account_id": str(source_account_id) if source_account_id else None,
                    "conversation_id": str(conversation_id) if conversation_id else None,
                },
                "upper": {
                    "committed_at": page.upper_bound[0].isoformat(),
                    "id": str(page.upper_bound[1]),
                },
                "after": {
                    "committed_at": last["committed_at"].isoformat(),
                    "id": str(last["conversation_observation_id"]),
                },
            },
            _cursor_secret(settings),
        )
    return ObservationPage(items=page.items, next_cursor=next_cursor, limit=limit)


@router.get(
    "/conversation-observations/{observation_id}/nodes",
    response_model=NodePage,
)
def list_conversation_observation_nodes(
    observation_id: uuid.UUID,
    response: Response,
    cursor: str | None = None,
    limit: int = Query(default=100, ge=1, le=100),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> NodePage:
    _private_no_store(response)
    after = None
    if cursor is not None:
        after = _decode_node_cursor(
            cursor,
            settings=settings,
            observation_id=observation_id,
        )
    try:
        page = EvidenceQueryService(db).list_observation_nodes(
            observation_id=observation_id,
            limit=limit,
            after=after,
        )
    except EvidenceIntegrityError as exc:
        request_id = uuid.uuid4().hex
        logger.error(
            "evidence query integrity violation",
            extra={
                "error_code": "evidence_integrity_violation",
                "error_type": type(exc).__name__,
                "operation": "evidence_query",
                "correlation_id": request_id,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "evidence_integrity_violation", "request_id": request_id},
        ) from exc
    if page is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "evidence_observation_not_found"},
        )
    if page.context is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "evidence_integrity_violation", "request_id": uuid.uuid4().hex},
        )
    next_cursor = None
    if page.has_more and page.items:
        last = page.items[-1]
        next_cursor = encode_cursor(
            {
                "kind": "observation-nodes",
                "observation_id": str(observation_id),
                "after": {
                    "observation_order": last["observation_order"],
                    "id": str(last["mapping_node_observation_id"]),
                },
            },
            _cursor_secret(settings),
        )
    return NodePage(
        observation=page.context,
        items=page.items,
        next_cursor=next_cursor,
        limit=limit,
    )


@router.get(
    "/conversation-observations/{observation_id}/attachments/{attachment_id}/observations",
    response_model=AttachmentObservationPage,
)
def list_attachment_observations(
    observation_id: uuid.UUID,
    attachment_id: uuid.UUID,
    response: Response,
    cursor: str | None = None,
    limit: int = Query(default=100, ge=1, le=100),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AttachmentObservationPage:
    _private_no_store(response)
    upper_bound = None
    after = None
    if cursor is not None:
        upper_bound, after = _decode_attachment_cursor(
            cursor,
            settings=settings,
            observation_id=observation_id,
            attachment_id=attachment_id,
        )
    page = EvidenceQueryService(db).list_attachment_observations(
        attachment_id=attachment_id,
        conversation_observation_id=observation_id,
        limit=limit,
        upper_bound=upper_bound,
        after=after,
    )
    if page is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "evidence_attachment_not_found"},
        )
    next_cursor = None
    if page.has_more and page.items and page.upper_bound is not None:
        last = page.items[-1]
        next_cursor = encode_cursor(
            {
                "kind": "attachment-observations",
                "observation_id": str(observation_id),
                "attachment_id": str(attachment_id),
                "upper": {
                    "attempt_number": page.upper_bound[0],
                    "observed_at": page.upper_bound[1].isoformat(),
                    "id": str(page.upper_bound[2]),
                },
                "after": {
                    "attempt_number": last["attempt_number"],
                    "observed_at": last["observed_at"].isoformat(),
                    "id": str(last["attachment_observation_id"]),
                },
            },
            _cursor_secret(settings),
        )
    return AttachmentObservationPage(
        attachment=page.attachment,
        items=page.items,
        next_cursor=next_cursor,
        limit=limit,
    )
