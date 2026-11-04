"""Canonical qingyu_native archive document.

The archived bytes are this document. payload_hash is their SHA-256.
client_request_id is not a field here: it is ledger identity only.
"""
import hashlib
import json
from typing import Any

MARKER = "qingyu-native-v1"
PROVENANCE = "client_declared"


def canonical_bytes(document: dict[str, Any]) -> bytes:
    """UTF-8 canonical JSON. Key order does not change the digest."""
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return encoded.encode("utf-8")


def payload_hash(document: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(document)).hexdigest()


def native_document(
    *,
    kind: str,
    source_platform: str,
    client_id: str,
    source_conversation_id: str,
    title: str | None,
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the closed document the parser accepts.

    ``client_id`` is the id from the ingest credential, never a body field.
    """
    return {
        "schema_marker": MARKER,
        "kind": kind,
        "provenance": PROVENANCE,
        "source_platform": source_platform,
        "client_id": client_id,
        "conversation": {
            "source_conversation_id": source_conversation_id,
            "title": title,
        },
        "messages": messages,
    }


def message_record(
    *,
    client_message_id: str,
    parent_client_message_id: str | None,
    role: str,
    content_text: str,
    content_kind: str,
    model: str | None,
    timestamp: str | None,
) -> dict[str, Any]:
    return {
        "client_message_id": client_message_id,
        "parent_client_message_id": parent_client_message_id,
        "role": role,
        "content_text": content_text,
        "content_kind": content_kind,
        "model": model,
        "timestamp": timestamp,
    }
