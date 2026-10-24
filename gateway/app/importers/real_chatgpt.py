from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from .generic import (
    ParsedAttachment,
    ParsedConversation,
    ParsedMessage,
    ParsedNode,
    ParserValidationError,
    pointer_escape,
)

FRAGMENT_FINGERPRINT_VERSION = "canonical-json-v1"
SEMANTIC_FINGERPRINT_VERSION = "chatgpt-message-v1"
OBSERVATION_FINGERPRINT_VERSION = "chatgpt-observation-v1"

SUPPORTED_CONTENT_TYPES = frozenset({
    "text",
    "multimodal_text",
    "thoughts",
    "reasoning_recap",
})

# These keys describe how the message was observed/exported.  They are retained
# in observation evidence but deliberately excluded from semantic revision
# identity.  In particular, short-lived asset URLs and decoding identifiers must
# not manufacture a new historical message revision.
OBSERVATION_ONLY_CONTENT_KEYS = frozenset({"metadata", "expiry_datetime", "decoding_id"})


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def canonical_fragment_fingerprint(value: Any, pointer: str) -> str:
    return fingerprint({"version": FRAGMENT_FINGERPRINT_VERSION, "pointer": pointer, "value": value})


def normalize_timestamp(value: Any) -> tuple[datetime | None, str]:
    if value is None:
        return None, "missing"
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None, "unsupported_type"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, "invalid"
    if not math.isfinite(number):
        return None, "invalid"
    try:
        return datetime.fromtimestamp(number, tz=UTC), "parsed"
    except (OverflowError, OSError, ValueError):
        return None, "out_of_range"


def _non_finite_pointers(value: Any, pointer: str = "") -> Iterator[str]:
    if isinstance(value, float) and not math.isfinite(value):
        yield pointer
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _non_finite_pointers(child, f"{pointer}/{pointer_escape(key)}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _non_finite_pointers(child, f"{pointer}/{index}")


def _semantic_content(value: Any) -> Any:
    """Remove only explicitly classified observation fields from content.

    Unknown content-bearing fields remain semantic.  This keeps future metadata
    additions forward compatible without silently discarding new actual content.
    """
    if isinstance(value, dict):
        return {
            key: _semantic_content(child)
            for key, child in value.items()
            if key not in OBSERVATION_ONLY_CONTENT_KEYS
        }
    if isinstance(value, list):
        return [_semantic_content(child) for child in value]
    return copy.deepcopy(value)


def _asset_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    prefix = "sediment://"
    if not value.startswith(prefix):
        return None
    identity = value[len(prefix):]
    if not identity or "/" in identity or "\\" in identity or identity in {".", ".."}:
        return None
    return identity


def _pointer_references(value: Any, pointer: str) -> list[tuple[str | None, dict[str, Any]]]:
    found: list[tuple[str | None, dict[str, Any]]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_pointer = f"{pointer}/{pointer_escape(key)}"
            if key == "asset_pointer":
                values = child if isinstance(child, list) else [child]
                for index, candidate in enumerate(values):
                    candidate_pointer = f"{child_pointer}/{index}" if isinstance(child, list) else child_pointer
                    if isinstance(candidate, str):
                        found.append((_asset_id(candidate), {"kind": key, "value": candidate, "raw_json_pointer": candidate_pointer}))
            found.extend(_pointer_references(child, child_pointer))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_pointer_references(child, f"{pointer}/{index}"))
    return found


def _attachments(message: dict[str, Any], pointer: str) -> tuple[ParsedAttachment, ...]:
    grouped: dict[str, dict[str, Any]] = {}
    anonymous_occurrences: dict[str, int] = {}
    metadata = message.get("metadata")
    descriptors = metadata.get("attachments") if isinstance(metadata, dict) else None
    if isinstance(descriptors, list):
        for index, descriptor in enumerate(descriptors):
            if not isinstance(descriptor, dict):
                continue
            raw_pointer = f"{pointer}/metadata/attachments/{index}"
            identity = descriptor.get("id") if isinstance(descriptor.get("id"), str) else None
            descriptor_fingerprint = fingerprint(descriptor)
            occurrence = anonymous_occurrences.get(descriptor_fingerprint, 0)
            anonymous_occurrences[descriptor_fingerprint] = occurrence + 1
            key = f"asset:{identity}" if identity else f"descriptor:{descriptor_fingerprint}:{occurrence}"
            entry = grouped.setdefault(key, {"descriptor": copy.deepcopy(descriptor), "asset_id": identity, "references": []})
            entry["references"].append({"kind": "descriptor", "value": copy.deepcopy(descriptor), "raw_json_pointer": raw_pointer})
    content = message.get("content")
    for identity, reference in _pointer_references(content, f"{pointer}/content"):
        if identity is None:
            key = f"unsupported:{fingerprint(reference)}"
        else:
            key = f"asset:{identity}"
        entry = grouped.setdefault(key, {"descriptor": {}, "asset_id": identity, "references": []})
        entry["references"].append(reference)

    output = []
    for key in sorted(grouped):
        entry = grouped[key]
        descriptor = entry["descriptor"]
        identity = entry["asset_id"]
        references = tuple(sorted(entry["references"], key=lambda item: item["raw_json_pointer"]))
        output.append(ParsedAttachment(
            stable_attachment_key=key,
            external_attachment_id=identity,
            original_reference=next(
                (r.get("value") for r in references if isinstance(r.get("value"), str)),
                None,
            ),
            member_path=None,
            filename=descriptor.get("name") if isinstance(descriptor.get("name"), str) else None,
            media_type=descriptor.get("mime_type") if isinstance(descriptor.get("mime_type"), str) else None,
            size_bytes=descriptor.get("size") if isinstance(descriptor.get("size"), int) and not isinstance(descriptor.get("size"), bool) else None,
            metadata=copy.deepcopy(descriptor),
            descriptor_json=copy.deepcopy(descriptor),
            raw_json_pointer=references[0]["raw_json_pointer"],
            asset_id=identity,
            source_references=references,
            source_fragment_json=copy.deepcopy(references[0].get("value")),
        ))
    return tuple(output)


class ChatGPTExportParser:
    """Strict parser for the supported official ChatGPT export v1 structure."""

    name = "chatgpt-export"
    version = "1"

    def can_parse(self, payload: Any) -> bool:
        if not isinstance(payload, list) or not payload:
            return False
        # Detection establishes the document family from a minimal signature.
        # Full validation deliberately happens separately and checks *every*
        # conversation before persistence, so a malformed later item cannot turn
        # into a partially imported Export.
        first = payload[0]
        return (
            isinstance(first, dict)
            and isinstance(first.get("mapping"), dict)
            and isinstance(first.get("id") or first.get("conversation_id"), str)
        )

    def validate(self, payload: Any, member: str = "conversations.json") -> list[dict]:
        errors: list[dict[str, Any]] = []
        if not isinstance(payload, list):
            raise ParserValidationError([{"type": "invalid_top_level", "raw_pointer": "", "detail": "expected conversation array"}])
        if not payload:
            raise ParserValidationError([{"type": "empty_export", "raw_pointer": "", "detail": "expected at least one conversation"}])
        errors.extend(
            {
                "type": "invalid_non_finite_number",
                "raw_pointer": pointer,
                "detail": "JSON numbers must be finite",
            }
            for pointer in _non_finite_pointers(payload)
        )
        conversation_ids: dict[str, str] = {}
        for conversation_index, conversation in enumerate(payload):
            base = f"/{conversation_index}"
            if not isinstance(conversation, dict):
                errors.append({"type": "invalid_conversation", "raw_pointer": base, "detail": "expected object"})
                continue
            native_id = conversation.get("id")
            conversation_id = conversation.get("conversation_id")
            if native_id is not None and (not isinstance(native_id, str) or not native_id):
                errors.append({"type": "invalid_conversation_id", "raw_pointer": f"{base}/id", "detail": "expected non-empty string or null"})
            if conversation_id is not None and (not isinstance(conversation_id, str) or not conversation_id):
                errors.append({"type": "invalid_conversation_id", "raw_pointer": f"{base}/conversation_id", "detail": "expected non-empty string or null"})
            if isinstance(native_id, str) and isinstance(conversation_id, str) and native_id != conversation_id:
                errors.append({"type": "conflicting_conversation_ids", "raw_pointer": base, "detail": "id and conversation_id disagree"})
            external_id = native_id or conversation_id
            if not isinstance(external_id, str) or not external_id:
                errors.append({"type": "invalid_conversation_id", "raw_pointer": base, "detail": "expected source id"})
            elif external_id in conversation_ids:
                errors.append({"type": "duplicate_conversation_id", "raw_pointer": base, "detail": f"conversation id already declared at {conversation_ids[external_id]}"})
            else:
                conversation_ids[external_id] = base
            mapping = conversation.get("mapping")
            if not isinstance(mapping, dict):
                errors.append({"type": "invalid_mapping", "raw_pointer": f"{base}/mapping", "detail": "expected object"})
                continue
            if not mapping:
                errors.append({"type": "empty_mapping", "raw_pointer": f"{base}/mapping", "detail": "expected at least one mapping node"})
            current = conversation.get("current_node")
            if current is not None and not isinstance(current, str):
                errors.append({"type": "invalid_current_node", "raw_pointer": f"{base}/current_node", "detail": "expected string or null"})
            parents: dict[str, str | None] = {}
            declared_children: dict[str, list[str]] = {}
            for node_key, node in mapping.items():
                pointer = f"{base}/mapping/{pointer_escape(node_key)}"
                if not isinstance(node_key, str) or not node_key:
                    errors.append({"type": "invalid_node_key", "raw_pointer": pointer, "detail": "expected non-empty string"})
                    continue
                if not isinstance(node, dict):
                    errors.append({"type": "invalid_node", "raw_pointer": pointer, "detail": "expected object"})
                    continue
                declared_id = node.get("id")
                if declared_id is not None:
                    if not isinstance(declared_id, str) or not declared_id:
                        errors.append({"type": "invalid_node_id", "raw_pointer": f"{pointer}/id", "detail": "expected non-empty string or null"})
                    elif declared_id != node_key:
                        errors.append({"type": "mapping_node_id_mismatch", "raw_pointer": f"{pointer}/id", "detail": "declared id differs from mapping key"})
                parent = node.get("parent")
                if parent is not None and not isinstance(parent, str):
                    errors.append({"type": "invalid_parent", "raw_pointer": f"{pointer}/parent", "detail": "expected string or null"})
                parents[node_key] = parent if isinstance(parent, str) else None
                if "children" in node:
                    children = node["children"]
                    if not isinstance(children, list) or any(not isinstance(child, str) for child in children):
                        errors.append({"type": "invalid_children", "raw_pointer": f"{pointer}/children", "detail": "expected string array"})
                    else:
                        declared_children[node_key] = children
                        if len(children) != len(set(children)):
                            errors.append({"type": "duplicate_declared_child", "raw_pointer": f"{pointer}/children", "detail": "children must be unique"})
                message = node.get("message")
                if "message" in node and message is not None and not isinstance(message, dict):
                    errors.append({"type": "invalid_message", "raw_pointer": f"{pointer}/message", "detail": "expected object or null"})
                if isinstance(message, dict):
                    self._validate_message(message, f"{pointer}/message", errors)
            for node_key, parent in parents.items():
                if parent == node_key:
                    errors.append({"type": "self_parent", "raw_pointer": f"{base}/mapping/{pointer_escape(node_key)}/parent", "detail": "node is its own parent"})
                elif parent is not None and parent not in mapping:
                    errors.append({"type": "missing_parent", "raw_pointer": f"{base}/mapping/{pointer_escape(node_key)}/parent", "detail": "parent not found"})
            for node_key in parents:
                seen: set[str] = set()
                cursor: str | None = node_key
                while cursor is not None and cursor in parents:
                    if cursor in seen:
                        errors.append({"type": "cycle", "raw_pointer": f"{base}/mapping/{pointer_escape(node_key)}", "detail": "cycle detected"})
                        break
                    seen.add(cursor)
                    cursor = parents[cursor]
            roots = [node_key for node_key, parent in parents.items() if parent is None]
            if mapping and len(roots) != 1:
                errors.append({"type": "invalid_root_count", "raw_pointer": f"{base}/mapping", "detail": "expected exactly one root"})
            for parent, children in declared_children.items():
                derived = [key for key, value in parents.items() if value == parent]
                # The explicit list owns sibling order.  Mapping object order is
                # retained separately as observation_order and need not match it.
                if set(children) != set(derived):
                    errors.append({"type": "children_parent_disagreement", "raw_pointer": f"{base}/mapping/{pointer_escape(parent)}/children", "detail": "children disagree with parent edges"})
            if isinstance(current, str) and current not in mapping:
                errors.append({"type": "missing_current_node", "raw_pointer": f"{base}/current_node", "detail": "current node not found"})
        if errors:
            raise ParserValidationError(errors)
        return []

    def _validate_message(self, message: dict[str, Any], pointer: str, errors: list[dict[str, Any]]) -> None:
        message_id = message.get("id")
        if message_id is not None and (not isinstance(message_id, str) or not message_id):
            errors.append({"type": "invalid_message_id", "raw_pointer": f"{pointer}/id", "detail": "expected non-empty string or null"})

        author = message.get("author")
        if not isinstance(author, dict) or not isinstance(author.get("role"), str):
            errors.append({"type": "invalid_author", "raw_pointer": f"{pointer}/author", "detail": "expected author role"})
        elif "name" in author and author["name"] is not None and not isinstance(author["name"], str):
            errors.append({"type": "invalid_author_name", "raw_pointer": f"{pointer}/author/name", "detail": "expected string or null"})

        content = message.get("content")
        content_type = content.get("content_type") if isinstance(content, dict) else None
        if not isinstance(content, dict) or not isinstance(content_type, str):
            errors.append({"type": "invalid_content", "raw_pointer": f"{pointer}/content", "detail": "expected content object and type"})
        elif content_type not in SUPPORTED_CONTENT_TYPES:
            errors.append({"type": "unsupported_content_type", "raw_pointer": f"{pointer}/content/content_type", "detail": "unsupported structural content variant"})
        elif content_type in {"text", "multimodal_text"}:
            parts = content.get("parts")
            if not isinstance(parts, list):
                errors.append({"type": "invalid_content_parts", "raw_pointer": f"{pointer}/content/parts", "detail": "expected array"})
            elif content_type == "text" and any(not isinstance(part, str) for part in parts):
                errors.append({"type": "invalid_text_part", "raw_pointer": f"{pointer}/content/parts", "detail": "text parts must be strings"})
            elif content_type == "multimodal_text" and any(not isinstance(part, (str, dict)) for part in parts):
                errors.append({"type": "invalid_multimodal_part", "raw_pointer": f"{pointer}/content/parts", "detail": "multimodal parts must be strings or objects"})
        elif content_type == "reasoning_recap" and not isinstance(content.get("content"), str):
            errors.append({"type": "invalid_reasoning_recap", "raw_pointer": f"{pointer}/content/content", "detail": "expected string"})
        elif content_type == "thoughts":
            thoughts = content.get("thoughts")
            if not isinstance(thoughts, list) or any(not isinstance(item, dict) for item in thoughts):
                errors.append({"type": "invalid_thoughts", "raw_pointer": f"{pointer}/content/thoughts", "detail": "expected object array"})
            source_id = content.get("source_analysis_msg_id")
            if not isinstance(source_id, str) or not source_id:
                errors.append({"type": "invalid_source_analysis_msg_id", "raw_pointer": f"{pointer}/content/source_analysis_msg_id", "detail": "expected non-empty string"})

        metadata = message.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            errors.append({"type": "invalid_message_metadata", "raw_pointer": f"{pointer}/metadata", "detail": "expected object or null"})
        if isinstance(metadata, dict) and "model_slug" in metadata and metadata["model_slug"] is not None and not isinstance(metadata["model_slug"], str):
            errors.append({"type": "invalid_model_slug", "raw_pointer": f"{pointer}/metadata/model_slug", "detail": "expected string or null"})
        attachments = metadata.get("attachments") if isinstance(metadata, dict) else None
        if attachments is not None:
            if not isinstance(attachments, list) or any(not isinstance(item, dict) for item in attachments):
                errors.append({"type": "invalid_attachments", "raw_pointer": f"{pointer}/metadata/attachments", "detail": "expected descriptor array"})
            else:
                descriptor_ids: dict[str, tuple[str, dict[str, Any]]] = {}
                for index, descriptor in enumerate(attachments):
                    descriptor_pointer = f"{pointer}/metadata/attachments/{index}"
                    identity = descriptor.get("id")
                    if identity is not None and (not isinstance(identity, str) or not identity):
                        errors.append({"type": "invalid_attachment_id", "raw_pointer": f"{descriptor_pointer}/id", "detail": "expected non-empty string or null"})
                    for field in ("name", "mime_type"):
                        if field in descriptor and descriptor[field] is not None and not isinstance(descriptor[field], str):
                            errors.append({"type": f"invalid_attachment_{field}", "raw_pointer": f"{descriptor_pointer}/{field}", "detail": "expected string or null"})
                    size = descriptor.get("size")
                    if "size" in descriptor and size is not None and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
                        errors.append({"type": "invalid_attachment_size", "raw_pointer": f"{descriptor_pointer}/size", "detail": "expected non-negative integer or null"})
                    if isinstance(identity, str) and identity:
                        previous = descriptor_ids.get(identity)
                        if previous is not None and previous[1] != descriptor:
                            errors.append({"type": "conflicting_attachment_descriptor", "raw_pointer": descriptor_pointer, "detail": f"attachment id already has a different descriptor at {previous[0]}"})
                        else:
                            descriptor_ids[identity] = (descriptor_pointer, descriptor)

        for field in ("create_time", "update_time"):
            value = message.get(field)
            if field in message and value is not None and (isinstance(value, bool) or not isinstance(value, (str, int, float))):
                errors.append({"type": f"invalid_{field}", "raw_pointer": f"{pointer}/{field}", "detail": "expected string, number, or null"})
            elif isinstance(value, float) and not math.isfinite(value):
                errors.append({"type": f"invalid_{field}", "raw_pointer": f"{pointer}/{field}", "detail": "expected finite number"})
        for field in ("status", "recipient", "channel"):
            value = message.get(field)
            if field in message and value is not None and not isinstance(value, str):
                errors.append({"type": f"invalid_{field}", "raw_pointer": f"{pointer}/{field}", "detail": "expected string or null"})
        if "end_turn" in message and message["end_turn"] is not None and not isinstance(message["end_turn"], bool):
            errors.append({"type": "invalid_end_turn", "raw_pointer": f"{pointer}/end_turn", "detail": "expected boolean or null"})
        weight = message.get("weight")
        if "weight" in message and weight is not None and (isinstance(weight, bool) or not isinstance(weight, (int, float))):
            errors.append({"type": "invalid_weight", "raw_pointer": f"{pointer}/weight", "detail": "expected number or null"})
        elif isinstance(weight, float) and not math.isfinite(weight):
            errors.append({"type": "invalid_weight", "raw_pointer": f"{pointer}/weight", "detail": "expected finite number"})

    def parse(self, payload: list[dict[str, Any]]) -> Iterator[ParsedConversation]:
        self.validate(payload)
        for conversation_index, conversation in enumerate(payload):
            base = f"/{conversation_index}"
            nodes: list[ParsedNode] = []
            messages: list[ParsedMessage] = []
            for order, (node_id, node) in enumerate(conversation["mapping"].items()):
                pointer = f"{base}/mapping/{pointer_escape(node_id)}"
                raw_message = node.get("message")
                parsed_message = self._parse_message(
                    raw_message,
                    node_id,
                    node.get("parent") if isinstance(node.get("parent"), str) else None,
                    f"{pointer}/message",
                ) if isinstance(raw_message, dict) else None
                if parsed_message is not None:
                    messages.append(parsed_message)
                node_envelope = {key: copy.deepcopy(value) for key, value in node.items() if key != "message"}
                nodes.append(ParsedNode(
                    node_id=node_id,
                    declared_node_id=node.get("id") if isinstance(node.get("id"), str) else None,
                    parent_node_id=node.get("parent") if isinstance(node.get("parent"), str) else None,
                    declared_children=tuple(node["children"]) if isinstance(node.get("children"), list) else None,
                    message_presence="object" if isinstance(raw_message, dict) else ("null" if "message" in node else "absent"),
                    message=parsed_message,
                    raw_json_pointer=pointer,
                    source_node_json=node_envelope,
                    source_fragment_json=copy.deepcopy(node),
                ))
            envelope = {key: copy.deepcopy(value) for key, value in conversation.items() if key != "mapping"}
            yield ParsedConversation(
                external_id=conversation.get("id") or conversation.get("conversation_id"),
                title=conversation.get("title") if isinstance(conversation.get("title"), str) else None,
                messages=tuple(messages),
                current_node_id=conversation.get("current_node") if isinstance(conversation.get("current_node"), str) else None,
                nodes=tuple(nodes),
                raw_json_pointer=base,
                source_conversation_json=envelope,
                raw_create_timestamp=conversation.get("create_time"),
                raw_update_timestamp=conversation.get("update_time"),
                source_fragment_json=copy.deepcopy(conversation),
            )

    def _parse_message(
        self,
        message: dict[str, Any],
        node_id: str,
        parent_node_id: str | None,
        pointer: str,
    ) -> ParsedMessage:
        author = message.get("author") if isinstance(message.get("author"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), dict) else {}
        raw_metadata = message.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        parts = content.get("parts")
        text_parts = [part for part in parts if isinstance(part, str)] if isinstance(parts, list) else []
        content_text = "\n".join(text_parts) if text_parts else None
        external_id = message.get("id") if isinstance(message.get("id"), str) and message.get("id") else f"synthetic:node:{node_id}"
        parsed_attachments = _attachments(message, pointer)
        semantic_author = {
            key: copy.deepcopy(author[key])
            for key in ("role", "name")
            if key in author
        }
        semantic_metadata = {key: copy.deepcopy(metadata[key]) for key in ("model_slug",) if key in metadata}
        semantic = {
            "author": semantic_author,
            "content": _semantic_content(content),
            "semantic_metadata": semantic_metadata,
            "attachments": [copy.deepcopy(item.descriptor_json) for item in parsed_attachments],
        }
        for key in ("create_time", "update_time", "status", "end_turn", "weight", "recipient", "channel"):
            if key in message:
                semantic[key] = copy.deepcopy(message[key])
        observation = {
            "message_id_presence": (
                "string" if isinstance(message.get("id"), str) and message.get("id")
                else "null" if "id" in message
                else "absent"
            ),
            "author": copy.deepcopy(author),
            "content": copy.deepcopy(content),
            "metadata": copy.deepcopy(raw_metadata),
            "metadata_presence": (
                "object" if isinstance(raw_metadata, dict)
                else "null" if "metadata" in message
                else "absent"
            ),
            "unknown_message_fields": {
                key: copy.deepcopy(value)
                for key, value in message.items()
                if key not in {"id", "author", "content", "create_time", "update_time", "status", "end_turn", "weight", "recipient", "channel", "metadata"}
            },
        }
        return ParsedMessage(
            node_id=node_id,
            parent_node_id=parent_node_id,
            role=str(author.get("role", "unknown")),
            content_text=content_text,
            content_json=copy.deepcopy(content),
            model=metadata.get("model_slug") if isinstance(metadata.get("model_slug"), str) else None,
            raw_timestamp=str(message.get("create_time")) if message.get("create_time") is not None else None,
            branch_key=None,
            attachments=parsed_attachments,
            external_message_id=external_id,
            raw_update_timestamp=copy.deepcopy(message.get("update_time")),
            content_type=content.get("content_type") if isinstance(content.get("content_type"), str) else None,
            semantic_evidence_json=semantic,
            observation_evidence_json=observation,
            raw_json_pointer=pointer,
            source_fragment_json=copy.deepcopy(message),
        )
