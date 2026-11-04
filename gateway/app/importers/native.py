"""Parser for one qingyu_native archive document.

Each document is an append_delta: a new observation of the messages in that
request, not a rewrite of an earlier observation. A parent that is absent
from this document is a normal delta edge. The import service records that
as info and keeps external_parent_node_id.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, ClassVar

from ..ingest.canonical import MARKER, PROVENANCE
from .generic import (
    ParsedConversation,
    ParsedMessage,
    ParsedNode,
    ParserValidationError,
    pointer_escape,
)

PARSER_NAME = "qingyu_native"
PARSER_VERSION = "1"
NATIVE_OBSERVATION_FINGERPRINT_VERSION = "qingyu-native-observation-v1"

CONTENT_KINDS = frozenset({
    "conversation_message",
    "client_generated_summary",
    "user_note",
})
ROLES = frozenset({"user", "assistant", "system", "tool"})
_MAX_MESSAGES = 100
_MAX_TEXT = 100_000


def parse_native_timestamp(value: str | None) -> tuple[datetime | None, str]:
    """Parse an aware ISO-8601 timestamp. Naive values stay unparsed."""
    if value is None or value == "":
        return None, "missing"
    if not isinstance(value, str):
        return None, "invalid"
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None, "invalid"
    if parsed.tzinfo is None:
        return None, "naive"
    return parsed.astimezone(UTC), "parsed"


class QingyuNativeParser:
    """One native document in, one ParsedConversation out."""

    name = PARSER_NAME
    version = PARSER_VERSION
    append_delta = True
    top_fields: ClassVar[set[str]] = {
        "schema_marker",
        "kind",
        "provenance",
        "source_platform",
        "client_id",
        "conversation",
        "messages",
    }
    conversation_fields: ClassVar[set[str]] = {"source_conversation_id", "title"}
    message_fields: ClassVar[set[str]] = {
        "client_message_id",
        "parent_client_message_id",
        "role",
        "content_text",
        "content_kind",
        "model",
        "timestamp",
    }

    def can_parse(self, payload: Any) -> bool:
        return (
            isinstance(payload, dict)
            and payload.get("schema_marker") == MARKER
            and payload.get("kind") in {"conversation", "messages"}
            and isinstance(payload.get("conversation"), dict)
            and isinstance(payload.get("messages"), list)
        )

    def validate(self, payload: Any, member: str = "native.json") -> list[dict]:
        errors: list[dict] = []
        if not isinstance(payload, dict):
            raise ParserValidationError([{
                "type": "invalid_top_level",
                "raw_pointer": "",
                "detail": "expected JSON object",
            }])
        if payload.get("schema_marker") != MARKER:
            _error(errors, "invalid_schema_marker", "/schema_marker", "unsupported native marker")
        kind = payload.get("kind")
        if kind not in {"conversation", "messages"}:
            _error(errors, "invalid_kind", "/kind", "expected conversation or messages")
        if payload.get("provenance") != PROVENANCE:
            _error(errors, "invalid_provenance", "/provenance", "expected client_declared")
        self._require_str(payload, "source_platform", errors, "")
        self._require_str(payload, "client_id", errors, "")
        for field in payload.keys() - self.top_fields:
            _error(errors, "unknown_field", f"/{pointer_escape(field)}", "field is not in the native contract")
        conversation = payload.get("conversation")
        if not isinstance(conversation, dict):
            _error(errors, "invalid_conversation", "/conversation", "expected object")
            conversation = {}
        else:
            for field in conversation.keys() - self.conversation_fields:
                _error(
                    errors,
                    "unknown_field",
                    f"/conversation/{pointer_escape(field)}",
                    "field is not in the native contract",
                )
            self._require_str(conversation, "source_conversation_id", errors, "/conversation")
            title = conversation.get("title")
            if title is not None and not isinstance(title, str):
                _error(errors, "invalid_title", "/conversation/title", "expected string or null")
        messages = payload.get("messages")
        if not isinstance(messages, list):
            _error(errors, "invalid_messages", "/messages", "expected array")
            messages = []
        if kind == "conversation" and messages:
            _error(errors, "unexpected_messages", "/messages", "a conversation create has no messages")
        if kind == "messages" and not messages:
            _error(errors, "missing_messages", "/messages", "expected at least one message")
        if len(messages) > _MAX_MESSAGES:
            _error(errors, "too_many_messages", "/messages", "at most 100 messages")
        seen: dict[str, str] = {}
        for index, message in enumerate(messages):
            base = f"/messages/{index}"
            if not isinstance(message, dict):
                _error(errors, "invalid_message", base, "expected object")
                continue
            for field in message.keys() - self.message_fields:
                _error(
                    errors,
                    "unknown_field",
                    f"{base}/{pointer_escape(field)}",
                    "field is not in the native contract",
                )
            message_id = message.get("client_message_id")
            if not isinstance(message_id, str) or not message_id.strip():
                _error(errors, "invalid_client_message_id", f"{base}/client_message_id", "expected string")
            elif message_id in seen:
                _error(
                    errors,
                    "duplicate_client_message_id",
                    f"{base}/client_message_id",
                    f"already declared at {seen[message_id]}",
                )
            else:
                seen[message_id] = f"{base}/client_message_id"
            parent = message.get("parent_client_message_id")
            if parent is not None and not isinstance(parent, str):
                _error(errors, "invalid_parent", f"{base}/parent_client_message_id", "expected string or null")
            elif isinstance(parent, str) and parent == message_id:
                _error(errors, "self_parent", f"{base}/parent_client_message_id", "message cannot parent itself")
            role = message.get("role")
            if role not in ROLES:
                _error(errors, "invalid_role", f"{base}/role", "unsupported role")
            content_kind = message.get("content_kind")
            if content_kind not in CONTENT_KINDS:
                _error(errors, "invalid_content_kind", f"{base}/content_kind", "unsupported content_kind")
            elif content_kind == "client_generated_summary" and role == "user":
                _error(
                    errors,
                    "summary_role_user",
                    f"{base}/content_kind",
                    "a summary cannot be role user",
                )
            content = message.get("content_text")
            if not isinstance(content, str) or len(content) > _MAX_TEXT:
                _error(errors, "invalid_content_text", f"{base}/content_text", "expected text")
            model = message.get("model")
            if model is not None and not isinstance(model, str):
                _error(errors, "invalid_model", f"{base}/model", "expected string or null")
            timestamp = message.get("timestamp")
            if timestamp is not None and not isinstance(timestamp, str):
                _error(errors, "invalid_timestamp", f"{base}/timestamp", "expected string or null")
            elif isinstance(timestamp, str):
                _parsed, status = parse_native_timestamp(timestamp)
                if status != "parsed":
                    _error(errors, "invalid_timestamp", f"{base}/timestamp", "expected an aware ISO-8601 timestamp")
        if errors:
            raise ParserValidationError(errors)
        return []

    @staticmethod
    def _require_str(value: dict, field: str, errors: list[dict], base: str) -> None:
        item = value.get(field)
        pointer = f"{base}/{pointer_escape(field)}" if base else f"/{pointer_escape(field)}"
        if not isinstance(item, str) or not item.strip():
            _error(errors, f"invalid_{field}", pointer, "expected string")

    def parse(self, payload: dict[str, Any]) -> Iterator[ParsedConversation]:
        self.validate(payload)
        conversation = payload["conversation"]
        messages = payload["messages"]
        parsed_messages = []
        nodes = []
        for index, message in enumerate(messages):
            message_id = message["client_message_id"]
            pointer = f"/messages/{index}"
            content_text = message["content_text"]
            semantic = {
                "content_kind": message["content_kind"],
                "provenance": PROVENANCE,
            }
            observation = {
                "provenance": PROVENANCE,
                "client_id": payload["client_id"],
                "content_kind": message["content_kind"],
            }
            parsed = ParsedMessage(
                node_id=message_id,
                parent_node_id=message.get("parent_client_message_id"),
                role=message["role"],
                content_text=content_text,
                content_json={"content_type": "text", "parts": [content_text]},
                model=message.get("model"),
                raw_timestamp=message.get("timestamp"),
                branch_key=None,
                external_message_id=message_id,
                content_type="text",
                semantic_evidence_json=semantic,
                observation_evidence_json=observation,
                raw_json_pointer=pointer,
                source_fragment_json=dict(message),
            )
            parsed_messages.append(parsed)
            nodes.append(ParsedNode(
                node_id=message_id,
                declared_node_id=message_id,
                parent_node_id=message.get("parent_client_message_id"),
                declared_children=None,
                message_presence="object",
                message=parsed,
                raw_json_pointer=pointer,
                source_node_json=dict(message),
                source_fragment_json=dict(message),
            ))
        title = conversation.get("title")
        envelope = {
            "schema_marker": MARKER,
            "observation_scope": "append_delta",
            "provenance": PROVENANCE,
            "source_platform": payload["source_platform"],
            "external_conversation_id": conversation["source_conversation_id"],
        }
        if isinstance(title, str):
            envelope["title"] = title
        raw_create = messages[0].get("timestamp") if messages else None
        raw_update = messages[-1].get("timestamp") if messages else None
        yield ParsedConversation(
            external_id=conversation["source_conversation_id"],
            title=title if isinstance(title, str) else None,
            messages=tuple(parsed_messages),
            current_node_id=parsed_messages[-1].node_id if parsed_messages else None,
            nodes=tuple(nodes),
            raw_json_pointer="/conversation",
            source_conversation_json=envelope,
            raw_create_timestamp=raw_create,
            raw_update_timestamp=raw_update,
            source_fragment_json=dict(conversation),
        )


def _error(errors: list[dict], error_type: str, pointer: str, detail: str) -> None:
    errors.append({"type": error_type, "raw_pointer": pointer, "detail": detail})
