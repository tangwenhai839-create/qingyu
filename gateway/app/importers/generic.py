import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, ClassVar


class ParserValidationError(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("parser_validation_error")


@dataclass(frozen=True)
class ParsedAttachment:
    stable_attachment_key: str
    external_attachment_id: str | None
    original_reference: str | None
    member_path: str | None
    filename: str | None
    media_type: str | None
    size_bytes: int | None
    metadata: Any
    descriptor_json: dict[str, Any]
    raw_json_pointer: str
    asset_id: str | None = None
    source_references: tuple[dict[str, Any], ...] = ()
    source_fragment_json: Any = None


@dataclass(frozen=True)
class ParsedMessage:
    node_id: str
    parent_node_id: str | None
    role: str
    content_text: str | None
    content_json: Any
    model: str | None
    raw_timestamp: str | None
    branch_key: str | None
    # Keep this last, with a default, so existing positional construction remains
    # compatible with the pre-attachment parser contract.
    attachments: tuple[ParsedAttachment, ...] = ()
    external_message_id: str | None = None
    raw_update_timestamp: Any = None
    content_type: str | None = None
    semantic_evidence_json: dict[str, Any] | None = None
    observation_evidence_json: dict[str, Any] | None = None
    raw_json_pointer: str | None = None
    source_fragment_json: dict[str, Any] | None = None


@dataclass(frozen=True)
class ParsedNode:
    node_id: str
    declared_node_id: str | None
    parent_node_id: str | None
    declared_children: tuple[str, ...] | None
    message_presence: str
    message: ParsedMessage | None
    raw_json_pointer: str
    source_node_json: dict[str, Any]
    source_fragment_json: dict[str, Any] | None = None


@dataclass(frozen=True)
class ParsedConversation:
    external_id: str | None
    title: str | None
    messages: tuple[ParsedMessage, ...]
    current_node_id: str | None
    nodes: tuple[ParsedNode, ...] = ()
    raw_json_pointer: str | None = None
    source_conversation_json: dict[str, Any] | None = None
    raw_create_timestamp: Any = None
    raw_update_timestamp: Any = None
    source_fragment_json: dict[str, Any] | None = None


class GenericTreeParser:
    """Parser for the project fixture contract; not a claim of ChatGPT compatibility."""
    name = "generic-tree-fixture"
    version = "0.1"
    marker = "qingyu-fixture-v1"
    top_fields: ClassVar[set[str]] = {"schema_marker", "conversations"}
    conversation_fields: ClassVar[set[str]] = {"id", "title", "current_node", "mapping", "metadata"}
    node_fields: ClassVar[set[str]] = {"id", "parent", "children", "message", "metadata"}
    message_fields: ClassVar[set[str]] = {
        "id", "author", "content", "metadata", "model", "create_time",
        "update_time", "attachments",
    }
    author_fields: ClassVar[set[str]] = {"role", "name", "metadata"}
    content_fields: ClassVar[set[str]] = {"content_type", "parts", "text", "metadata"}
    attachment_fields: ClassVar[set[str]] = {
        "id", "reference", "member_path", "filename", "media_type",
        "size_bytes", "metadata",
    }

    def can_parse(self, payload) -> bool:
        return isinstance(payload, dict) and payload.get("schema_marker") == self.marker and isinstance(payload.get("conversations"), list)

    def validate(self, payload: Any, member="fixture.json") -> list[dict]:
        errors = []
        unknown = []
        if not isinstance(payload, dict):
            raise ParserValidationError([{"type": "invalid_top_level", "raw_pointer": "", "detail": "expected JSON object"}])
        if payload.get("schema_marker") != self.marker:
            errors.append({"type": "invalid_schema_marker", "raw_pointer": "/schema_marker", "detail": "unsupported fixture marker"})
        conversations = payload.get("conversations")
        if not isinstance(conversations, list):
            errors.append({"type": "invalid_conversations", "raw_pointer": "/conversations", "detail": "expected array"})
            conversations = []
        self._unknown_fields(payload, self.top_fields, "", member, unknown)
        for index, conversation in enumerate(conversations):
            base = f"/conversations/{index}"
            if not isinstance(conversation, dict):
                self._error(errors, "invalid_conversation", base, "expected object")
                continue
            self._unknown_fields(conversation, self.conversation_fields, base, member, unknown)
            self._optional_type(conversation, "id", str, errors, base)
            self._optional_type(conversation, "title", str, errors, base)
            self._optional_type(conversation, "current_node", str, errors, base)
            self._optional_mapping(conversation, "metadata", errors, base)
            mapping = conversation.get("mapping")
            if not isinstance(mapping, dict):
                self._error(errors, "invalid_mapping", f"{base}/mapping", "expected object")
                continue
            declared_node_ids = {}
            for node_id, node in mapping.items():
                pointer = f"{base}/mapping/{pointer_escape(node_id)}"
                if not isinstance(node, dict):
                    self._error(errors, "invalid_node", pointer, "expected object")
                    continue
                self._unknown_fields(node, self.node_fields, pointer, member, unknown)
                self._optional_type(node, "id", str, errors, pointer)
                declared_node_id = node.get("id")
                if declared_node_id is not None:
                    if declared_node_id != node_id:
                        self._error(
                            errors,
                            "mapping_node_id_mismatch",
                            f"{pointer}/id",
                            "declared node id does not match mapping key",
                        )
                    previous = declared_node_ids.get(declared_node_id)
                    if previous is not None:
                        self._error(
                            errors,
                            "duplicate_node_id",
                            f"{pointer}/id",
                            f"node id already declared at {previous}",
                        )
                    else:
                        declared_node_ids[declared_node_id] = f"{pointer}/id"
                self._optional_type(node, "parent", str, errors, pointer)
                self._optional_mapping(node, "metadata", errors, pointer)
                self._string_list(node, "children", errors, pointer)
                message = node.get("message")
                if not isinstance(message, dict):
                    self._error(errors, "invalid_message", f"{pointer}/message", "expected object")
                    continue
                self._validate_message(message, pointer, member, errors, unknown)
        if errors:
            raise ParserValidationError(errors)
        return unknown

    def _validate_message(self, message, node_pointer, member, errors, unknown):
        base = f"{node_pointer}/message"
        self._unknown_fields(message, self.message_fields, base, member, unknown)
        self._optional_type(message, "id", str, errors, base)
        self._optional_type(message, "model", str, errors, base)
        self._optional_scalar(message, "create_time", errors, base)
        self._optional_scalar(message, "update_time", errors, base)
        self._optional_mapping(message, "metadata", errors, base)
        self._validate_attachments(message, base, member, errors, unknown)

        author = message.get("author")
        if not isinstance(author, dict):
            self._error(errors, "invalid_author", f"{base}/author", "expected object")
        else:
            self._unknown_fields(author, self.author_fields, f"{base}/author", member, unknown)
            self._optional_type(author, "role", str, errors, f"{base}/author")
            self._optional_type(author, "name", str, errors, f"{base}/author")
            self._optional_mapping(author, "metadata", errors, f"{base}/author")

        content = message.get("content")
        if not isinstance(content, dict):
            self._error(errors, "invalid_content", f"{base}/content", "expected object")
        else:
            self._unknown_fields(content, self.content_fields, f"{base}/content", member, unknown)
            self._optional_type(content, "content_type", str, errors, f"{base}/content")
            self._optional_type(content, "text", str, errors, f"{base}/content")
            self._optional_mapping(content, "metadata", errors, f"{base}/content")
            self._list_field(content, "parts", errors, f"{base}/content")

        metadata = message.get("metadata")
        if isinstance(metadata, dict):
            self._optional_type(metadata, "model_slug", str, errors, f"{base}/metadata")

    def _validate_attachments(self, message, base, member, errors, unknown):
        attachments = message.get("attachments")
        self._list_field(message, "attachments", errors, base)
        if not isinstance(attachments, list):
            return
        named_keys = {}
        anonymous_counts = {}
        for index, attachment in enumerate(attachments):
            pointer = f"{base}/attachments/{index}"
            if not isinstance(attachment, dict):
                self._error(errors, "invalid_attachment", pointer, "expected object")
                continue
            self._unknown_fields(attachment, self.attachment_fields, pointer, member, unknown)
            for field in ("id", "reference", "member_path", "filename", "media_type"):
                self._optional_type(attachment, field, str, errors, pointer)
            self._optional_mapping(attachment, "metadata", errors, pointer)
            size = attachment.get("size_bytes")
            if "size_bytes" in attachment and size is not None and (
                isinstance(size, bool) or not isinstance(size, int) or size < 0
            ):
                self._error(errors, "invalid_size_bytes", f"{pointer}/size_bytes", "expected non-negative integer or null")
            try:
                base_key, anonymous, _canonical = attachment_identity_components(attachment)
            except ValueError as exc:
                self._error(errors, str(exc), pointer, "attachment identity field is empty or invalid")
                continue
            if anonymous:
                occurrence = anonymous_counts.get(base_key, 0) + 1
                anonymous_counts[base_key] = occurrence
                # Identical anonymous descriptors are distinct source declarations.
                # Their occurrence suffix is stable under reordering among identical values.
                stable_attachment_key(attachment, occurrence)
            else:
                previous = named_keys.get(base_key)
                if previous is not None:
                    self._error(
                        errors,
                        "duplicate_attachment_identity",
                        pointer,
                        f"stable attachment key already declared at {previous}",
                    )
                else:
                    named_keys[base_key] = pointer

    @staticmethod
    def _error(errors, error_type, pointer, detail):
        errors.append({"type": error_type, "raw_pointer": pointer, "detail": detail})

    def _optional_type(self, value, field, expected, errors, base):
        if field in value and value[field] is not None and not isinstance(value[field], expected):
            self._error(errors, f"invalid_{field}", f"{base}/{pointer_escape(field)}", f"expected {expected.__name__} or null")

    def _optional_mapping(self, value, field, errors, base):
        if field in value and value[field] is not None and not isinstance(value[field], dict):
            self._error(errors, f"invalid_{field}", f"{base}/{pointer_escape(field)}", "expected object or null")

    def _list_field(self, value, field, errors, base):
        if field in value and value[field] is not None and not isinstance(value[field], list):
            self._error(errors, f"invalid_{field}", f"{base}/{pointer_escape(field)}", "expected array or null")

    def _string_list(self, value, field, errors, base):
        self._list_field(value, field, errors, base)
        if isinstance(value.get(field), list) and any(not isinstance(item, str) for item in value[field]):
            self._error(errors, f"invalid_{field}", f"{base}/{pointer_escape(field)}", "expected array of strings")

    def _optional_scalar(self, value, field, errors, base):
        item = value.get(field)
        if field in value and item is not None and (isinstance(item, bool) or not isinstance(item, (str, int, float))):
            self._error(errors, f"invalid_{field}", f"{base}/{pointer_escape(field)}", "expected string, number, or null")

    @staticmethod
    def _unknown_fields(value, allowed, base, member, unknown):
        for field in value.keys() - allowed:
            unknown.append({"type": "unknown_field", "severity": "warning", "file": member, "field": field, "raw_pointer": f"{base}/{pointer_escape(field)}"})

    def parse(self, payload: dict[str, Any]) -> Iterator[ParsedConversation]:
        self.validate(payload)
        for conversation_index, conversation in enumerate(payload["conversations"]):
            nodes=[]
            for node_id,node in conversation["mapping"].items():
                message=node["message"]; author=message.get("author") or {}; content=message.get("content") or {}; parts=content.get("parts")
                text="\n".join(str(part) for part in parts) if isinstance(parts,list) else (str(content) if content else None)
                raw_attachments = message.get("attachments") or []
                attachment_keys = stable_attachment_keys(raw_attachments)
                node_pointer = f"/conversations/{conversation_index}/mapping/{pointer_escape(node_id)}/message"
                attachments = tuple(
                    parsed_attachment(
                        attachment,
                        stable_key=attachment_keys[index],
                        raw_json_pointer=f"{node_pointer}/attachments/{index}",
                    )
                    for index, attachment in enumerate(raw_attachments)
                )
                nodes.append(ParsedMessage(str(node_id),node.get("parent"),str(author.get("role","unknown")),text,content,(message.get("metadata") or {}).get("model_slug"),str(message.get("create_time")) if message.get("create_time") is not None else None,None,attachments))
            yield ParsedConversation(conversation.get("id"),conversation.get("title"),tuple(nodes),conversation.get("current_node"))


class ParserRegistry:
    def __init__(self, parsers=None):
        if parsers is None:
            from .real_chatgpt import ChatGPTExportParser
            parsers = [GenericTreeParser(), ChatGPTExportParser()]
        self.parsers = list(parsers)
    def resolve(self, name: str, version: str):
        """Resolve one exact installed parser contract without fallback."""
        matches = [parser for parser in self.parsers if parser.name == name and parser.version == version]
        return matches[0] if len(matches) == 1 else None
    def detect(self, payload, member: str | None = None) -> tuple[str, Any]:
        matches=[parser for parser in self.parsers if parser.can_parse(payload)]
        if len(matches)>1: return "ambiguous_structure",None
        suspected_real_export = (
            isinstance(payload, dict) and "conversations" in payload
        ) or (
            isinstance(payload, list)
            and bool(payload)
            and isinstance(payload[0], dict)
            and bool({"id", "conversation_id", "mapping", "current_node"} & payload[0].keys())
        )
        if not matches:
            return ("unsupported_real_chatgpt_export" if suspected_real_export else "unsupported_structure"),None
        return "matched",matches[0]


def pointer_escape(value) -> str: return str(value).replace("~","~0").replace("/","~1")


class JSONObjectPairs(list):
    """Distinguish JSON objects from arrays while preserving duplicate keys."""


def load_json_document(raw: str, member: str = "fixture.json"):
    """Decode JSON without silently discarding duplicate object keys."""
    decoded = json.loads(raw, object_pairs_hook=JSONObjectPairs)
    errors = []

    def normalize(value, pointer=""):
        if isinstance(value, JSONObjectPairs):
            result = {}
            first_pointers = {}
            for key, child in value:
                child_pointer = f"{pointer}/{pointer_escape(key)}"
                if key in result:
                    errors.append({
                        "type": "duplicate_json_key",
                        "file": member,
                        "raw_pointer": child_pointer,
                        "detail": f"JSON object key already declared at {first_pointers[key]}",
                    })
                    continue
                first_pointers[key] = child_pointer
                result[key] = normalize(child, child_pointer)
            return result
        if isinstance(value, list):
            return [normalize(child, f"{pointer}/{index}") for index, child in enumerate(value)]
        return value

    normalized = normalize(decoded)
    if errors:
        raise ParserValidationError(errors)
    return normalized


def canonical_attachment_descriptor(attachment: dict) -> str:
    """Canonical source-declared fields only; excludes import-time observations."""
    descriptor = {
        field: attachment.get(field)
        for field in sorted(GenericTreeParser.attachment_fields)
        if field in attachment
    }
    if isinstance(descriptor.get("id"), str):
        descriptor["id"] = descriptor["id"].strip()
    if isinstance(descriptor.get("reference"), str):
        descriptor["reference"] = normalize_attachment_reference(descriptor["reference"])
    if isinstance(descriptor.get("member_path"), str):
        descriptor["member_path"] = normalize_attachment_member_path(descriptor["member_path"])
    return json.dumps(descriptor, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_attachment_reference(value: str) -> str:
    """Normalize a declared reference without applying URL-unsafe path folding."""
    return value.strip().replace("\\", "/")


def normalize_attachment_member_path(value: str) -> str:
    """Return a safe relative ZIP member path with deterministic separators."""
    raw = value.strip().replace("\\", "/")
    if raw.startswith("/") or ":" in raw:
        raise ValueError("invalid_attachment_member_path")
    parts = []
    for part in raw.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            raise ValueError("invalid_attachment_member_path")
        parts.append(part)
    if not parts:
        raise ValueError("invalid_attachment_member_path")
    return "/".join(parts)


def attachment_identity_components(attachment: dict) -> tuple[str, bool, str]:
    canonical = canonical_attachment_descriptor(attachment)
    for field, prefix in (("id", "external"), ("reference", "reference"), ("member_path", "member")):
        value = attachment.get(field)
        if isinstance(value, str):
            if field == "reference":
                normalized = normalize_attachment_reference(value)
            elif field == "member_path":
                normalized = normalize_attachment_member_path(value)
            else:
                normalized = value.strip()
            if not normalized:
                raise ValueError(f"invalid_attachment_{field}")
            return f"{prefix}:{normalized}", False, canonical
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"descriptor:{digest}", True, canonical


def stable_attachment_key(attachment: dict, occurrence: int = 1) -> str:
    """Return a source-stable identity without using the attachment array index."""
    base_key, anonymous, _canonical = attachment_identity_components(attachment)
    return f"{base_key}:occurrence:{occurrence}" if anonymous else base_key


def stable_attachment_keys(attachments: list[dict]) -> list[str]:
    """Assign deterministic occurrence suffixes only to identical anonymous descriptors."""
    counts = {}
    keys = []
    for attachment in attachments:
        base_key, anonymous, _canonical = attachment_identity_components(attachment)
        if anonymous:
            counts[base_key] = counts.get(base_key, 0) + 1
            keys.append(stable_attachment_key(attachment, counts[base_key]))
        else:
            keys.append(base_key)
    return keys


def parsed_attachment(attachment: dict, *, stable_key: str, raw_json_pointer: str) -> ParsedAttachment:
    """Split stable source declarations from attempt-scoped raw provenance."""
    return ParsedAttachment(
        stable_attachment_key=stable_key,
        external_attachment_id=(
            attachment["id"].strip()
            if isinstance(attachment.get("id"), str)
            else None
        ),
        original_reference=(
            normalize_attachment_reference(attachment["reference"])
            if isinstance(attachment.get("reference"), str)
            else None
        ),
        member_path=(
            normalize_attachment_member_path(attachment["member_path"])
            if isinstance(attachment.get("member_path"), str)
            else None
        ),
        filename=attachment.get("filename"),
        media_type=attachment.get("media_type"),
        size_bytes=attachment.get("size_bytes"),
        metadata=attachment.get("metadata"),
        descriptor_json=json.loads(canonical_attachment_descriptor(attachment)),
        raw_json_pointer=raw_json_pointer,
    )
