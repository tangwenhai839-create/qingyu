"""HTTP write API for qingyu_native ingest.

Authentication is INGEST_CLIENTS only. client_id comes from that credential.
The body supplies client_request_id, which the caller must reuse on retry.
payload_hash is computed here and compared on replay; it is never the id.
"""
from __future__ import annotations

import re
import secrets
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from .auth import require_ingest_client
from .config import get_settings
from .db import get_db
from .importers.native import parse_native_timestamp
from .ingest.canonical import canonical_bytes, message_record, native_document, payload_hash
from .ingest.service import read_ingest_request, submit_native_document

router = APIRouter(prefix="/api/v1/ingest")

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_PLATFORM = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_ROLES = frozenset({"user", "assistant", "system", "tool"})
_KINDS = frozenset({"conversation_message", "client_generated_summary", "user_note"})
_NO_STORE = {"Cache-Control": "private, no-store"}


class NativeMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_message_id: str
    parent_client_message_id: str | None = None
    role: str
    content_text: str = Field(min_length=1, max_length=100_000)
    content_kind: str
    model: str | None = Field(default=None, max_length=255)
    timestamp: str | None = None

    @field_validator("client_message_id", "parent_client_message_id")
    @classmethod
    def _identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if _ID.fullmatch(value) is None:
            raise ValueError("invalid_id")
        return value

    @field_validator("role")
    @classmethod
    def _role(cls, value: str) -> str:
        if value not in _ROLES:
            raise ValueError("invalid_role")
        return value

    @field_validator("content_kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if value not in _KINDS:
            raise ValueError("invalid_content_kind")
        return value

    @field_validator("timestamp")
    @classmethod
    def _timestamp(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _parsed, status = parse_native_timestamp(value)
        if status != "parsed":
            raise ValueError("invalid_timestamp")
        return value

    @model_validator(mode="after")
    def _summary_is_not_a_user_statement(self) -> NativeMessage:
        if self.content_kind == "client_generated_summary" and self.role == "user":
            raise ValueError("summary_role_user")
        return self


class ConversationIngest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_request_id: str
    source_platform: str
    source_conversation_id: str
    title: str | None = Field(default=None, max_length=500)

    @field_validator("client_request_id", "source_conversation_id")
    @classmethod
    def _identifier(cls, value: str) -> str:
        if _ID.fullmatch(value) is None:
            raise ValueError("invalid_id")
        return value

    @field_validator("source_platform")
    @classmethod
    def _platform(cls, value: str) -> str:
        if _PLATFORM.fullmatch(value) is None:
            raise ValueError("invalid_platform")
        return value

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class MessagesIngest(ConversationIngest):
    message: NativeMessage


class MessageBatchIngest(ConversationIngest):
    messages: list[NativeMessage] = Field(min_length=1, max_length=100)


def _json(status_code: int, body: dict) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=body, headers=_NO_STORE)


def _reject_hash_as_request_id(client_request_id: str, digest: str) -> JSONResponse | None:
    """payload_hash is a conflict check. It is not a legal request id."""
    if len(client_request_id) == len(digest) and secrets.compare_digest(client_request_id.lower(), digest):
        return _json(422, {"detail": {"code": "request_id_must_not_be_payload_hash"}})
    return None


def _submit(
    db: Session,
    *,
    client_id: str,
    client_request_id: str,
    source_platform: str,
    source_conversation_id: str,
    title: str | None,
    kind: str,
    messages: list[dict],
) -> JSONResponse:
    document = native_document(
        kind=kind,
        source_platform=source_platform,
        client_id=client_id,
        source_conversation_id=source_conversation_id,
        title=title,
        messages=messages,
    )
    seen: set[str] = set()
    for item in messages:
        message_id = item["client_message_id"]
        if message_id in seen or item["parent_client_message_id"] == message_id:
            return _json(422, {"detail": {"code": "ingest_request_invalid"}})
        seen.add(message_id)
    digest = payload_hash(document)
    rejected = _reject_hash_as_request_id(client_request_id, digest)
    if rejected is not None:
        return rejected
    settings = get_settings()
    result = submit_native_document(
        db,
        archive_root=Path(settings.archive_root),
        client_id=client_id,
        client_request_id=client_request_id,
        payload_hash=digest,
        document=canonical_bytes(document),
        kind=kind,
        source_platform=source_platform,
        wait_seconds=settings.ingest_sync_wait_seconds,
    )
    return _json(result.status_code, result.body)


def _message_dict(message: NativeMessage) -> dict:
    return message_record(
        client_message_id=message.client_message_id,
        parent_client_message_id=message.parent_client_message_id,
        role=message.role,
        content_text=message.content_text,
        content_kind=message.content_kind,
        model=message.model,
        timestamp=message.timestamp,
    )


@router.post("/conversations")
def ingest_conversation(
    body: ConversationIngest,
    client_id: Annotated[str, Depends(require_ingest_client)],
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    return _submit(
        db,
        client_id=client_id,
        client_request_id=body.client_request_id,
        source_platform=body.source_platform,
        source_conversation_id=body.source_conversation_id,
        title=body.title,
        kind="conversation",
        messages=[],
    )


@router.post("/messages")
def ingest_message(
    body: MessagesIngest,
    client_id: Annotated[str, Depends(require_ingest_client)],
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    return _submit(
        db,
        client_id=client_id,
        client_request_id=body.client_request_id,
        source_platform=body.source_platform,
        source_conversation_id=body.source_conversation_id,
        title=body.title,
        kind="messages",
        messages=[_message_dict(body.message)],
    )


@router.post("/messages:batch")
def ingest_message_batch(
    body: MessageBatchIngest,
    client_id: Annotated[str, Depends(require_ingest_client)],
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    seen: set[str] = set()
    for message in body.messages:
        if message.client_message_id in seen:
            return _json(422, {"detail": {"code": "ingest_request_invalid"}})
        seen.add(message.client_message_id)
    return _submit(
        db,
        client_id=client_id,
        client_request_id=body.client_request_id,
        source_platform=body.source_platform,
        source_conversation_id=body.source_conversation_id,
        title=body.title,
        kind="messages",
        messages=[_message_dict(message) for message in body.messages],
    )


@router.get("/requests/{client_request_id}")
def get_ingest_request(
    client_request_id: str,
    client_id: Annotated[str, Depends(require_ingest_client)],
    db: Annotated[Session, Depends(get_db)],
) -> JSONResponse:
    if _ID.fullmatch(client_request_id) is None:
        return _json(404, {"detail": {"code": "ingest_request_not_found"}})
    result = read_ingest_request(db, client_id=client_id, client_request_id=client_request_id)
    if result is None:
        return _json(404, {"detail": {"code": "ingest_request_not_found"}})
    return _json(result.status_code, result.body)
