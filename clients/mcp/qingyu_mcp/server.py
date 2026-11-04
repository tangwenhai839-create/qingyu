"""Four MCP tools. stdio only; streamable HTTP is a later phase.

search_evidence and read_evidence call the evidence HTTP API.
ingest_message and ingest_conversation call the native ingest HTTP API.
The tool schemas do not accept client_request_id or payload_hash. The
adapter assigns the request id and stores it before the POST.
"""

from __future__ import annotations

import json
import sys
import uuid
from collections.abc import Callable
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .config import ConfigError, configure_logging, load_settings
from .gateway import GatewayAdapter, GatewayCallError, GatewayResponse
from .pending import PendingLogError

_READ = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_WRITE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_KEY = Annotated[
    str,
    Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$",
        description=(
            "Stable name for this write. Reuse it on every retry and after a "
            "restart. It is not the gateway client_request_id and it must not "
            "be a payload hash."
        ),
    ),
]
INSTRUCTIONS = (
    "Qingyu Gateway stdio adapter. "
    "search_evidence calls POST /api/v1/evidence/search. "
    "read_evidence calls the GET /api/v1/evidence routes. "
    "Both are read-only and return the gateway JSON unchanged. "
    "ingest_message calls POST /api/v1/ingest/messages. "
    "ingest_conversation calls POST /api/v1/ingest/conversations. "
    "Those posts are the only writes. "
    "Reuse the same idempotency_key when retrying a write, including after "
    "this process restarts. The adapter stores that key's client_request_id "
    "in a local pending log and sends it to the gateway. "
    "Do not invent a payload hash as an id. "
    "A 202 ingest result means the import worker has not finished; call the "
    "same tool again with the same idempotency_key."
)
TOOL_NAMES = (
    "search_evidence",
    "read_evidence",
    "ingest_message",
    "ingest_conversation",
)


def build_server(adapter: GatewayAdapter) -> MCPServer:
    mcp = MCPServer(
        "qingyu-gateway",
        instructions=INSTRUCTIONS,
        version=__version__,
    )

    @mcp.tool(name="search_evidence", title="Search evidence", annotations=_READ)
    def search_evidence(
        q: Annotated[str, Field(min_length=1, max_length=2000, description="Substring to find in committed message text.")],
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
        source_platform: str | None = None,
        source_account_id: str | None = None,
        conversation_id: str | None = None,
        time_from: Annotated[str | None, Field(description="Inclusive ISO-8601 timestamp with a timezone. Sent as from.")] = None,
        time_to: Annotated[str | None, Field(description="Inclusive ISO-8601 timestamp with a timezone. Sent as to.")] = None,
        cursor: Annotated[str | None, Field(max_length=4096)] = None,
    ) -> dict[str, Any]:
        """Search committed message text. The body is the gateway evidence-search-v1 JSON. This does not write."""
        body: dict[str, Any] = {"q": q, "limit": limit}
        if source_platform is not None:
            body["source_platform"] = source_platform
        if source_account_id is not None:
            body["source_account_id"] = _uuid(source_account_id)
        if conversation_id is not None:
            body["conversation_id"] = _uuid(conversation_id)
        if time_from is not None:
            body["from"] = time_from
        if time_to is not None:
            body["to"] = time_to
        if cursor is not None:
            body["cursor"] = cursor
        return _call(lambda: adapter.search_evidence(body))

    @mcp.tool(name="read_evidence", title="Read evidence", annotations=_READ)
    def read_evidence(
        view: Literal["conversation_observations", "nodes", "attachment_observations"],
        conversation_observation_id: str | None = None,
        attachment_id: str | None = None,
        source_account_id: str | None = None,
        conversation_id: str | None = None,
        cursor: Annotated[str | None, Field(max_length=4096)] = None,
        limit: Annotated[int | None, Field(ge=1, le=100)] = None,
    ) -> dict[str, Any]:
        """Read one page of committed evidence. The body is the gateway evidence-query-v1 JSON. Pass next_cursor back as cursor."""
        params: dict[str, str | int] = {}
        if cursor is not None:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit
        if view == "conversation_observations":
            path = "/api/v1/evidence/conversation-observations"
            if source_account_id is not None:
                params["source_account_id"] = _uuid(source_account_id)
            if conversation_id is not None:
                params["conversation_id"] = _uuid(conversation_id)
        elif view == "nodes":
            if conversation_observation_id is None:
                raise ToolError("conversation_observation_id_required")
            path = (
                "/api/v1/evidence/conversation-observations/"
                f"{_uuid(conversation_observation_id)}/nodes"
            )
        else:
            if conversation_observation_id is None or attachment_id is None:
                raise ToolError("attachment_ids_required")
            path = (
                "/api/v1/evidence/conversation-observations/"
                f"{_uuid(conversation_observation_id)}/attachments/{_uuid(attachment_id)}/observations"
            )
        return _call(lambda: adapter.read_evidence(path, params))

    @mcp.tool(name="ingest_message", title="Ingest message", annotations=_WRITE)
    def ingest_message(
        idempotency_key: _KEY,
        source_platform: Annotated[str, Field(min_length=1, max_length=32)],
        source_conversation_id: Annotated[str, Field(min_length=1, max_length=200)],
        client_message_id: Annotated[str, Field(min_length=1, max_length=200)],
        role: Literal["user", "assistant", "system", "tool"],
        content_text: Annotated[str, Field(min_length=1, max_length=100_000)],
        content_kind: Literal["conversation_message", "client_generated_summary", "user_note"],
        title: Annotated[str | None, Field(max_length=500)] = None,
        parent_client_message_id: Annotated[str | None, Field(max_length=200)] = None,
        model: Annotated[str | None, Field(max_length=255)] = None,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        """Append one message through POST /api/v1/ingest/messages. Reuse idempotency_key on retry. 202 means the worker has not finished."""
        message: dict[str, Any] = {
            "client_message_id": client_message_id,
            "role": role,
            "content_text": content_text,
            "content_kind": content_kind,
        }
        if parent_client_message_id is not None:
            message["parent_client_message_id"] = parent_client_message_id
        if model is not None:
            message["model"] = model
        if timestamp is not None:
            message["timestamp"] = timestamp
        return _call(
            lambda: adapter.ingest_message(
                idempotency_key=idempotency_key,
                source_platform=source_platform,
                source_conversation_id=source_conversation_id,
                title=title,
                message=message,
            ),
            idempotency_key=idempotency_key,
        )

    @mcp.tool(name="ingest_conversation", title="Ingest conversation", annotations=_WRITE)
    def ingest_conversation(
        idempotency_key: _KEY,
        source_platform: Annotated[str, Field(min_length=1, max_length=32)],
        source_conversation_id: Annotated[str, Field(min_length=1, max_length=200)],
        title: Annotated[str | None, Field(max_length=500)] = None,
    ) -> dict[str, Any]:
        """Record an empty conversation through POST /api/v1/ingest/conversations. Reuse idempotency_key on retry."""
        return _call(
            lambda: adapter.ingest_conversation(
                idempotency_key=idempotency_key,
                source_platform=source_platform,
                source_conversation_id=source_conversation_id,
                title=title,
            ),
            idempotency_key=idempotency_key,
        )

    return mcp


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        sys.stderr.write(f"{exc}\n")
        raise SystemExit(2) from None
    configure_logging(settings)
    adapter = GatewayAdapter(settings)
    try:
        build_server(adapter).run(transport="stdio")
    finally:
        adapter.close()


def _call(
    fn: Callable[[], GatewayResponse],
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    try:
        response = fn()
    except (GatewayCallError, PendingLogError) as exc:
        raise ToolError(_error_payload(str(exc), idempotency_key)) from None
    return _finish(response, idempotency_key=idempotency_key)


def _finish(response: GatewayResponse, *, idempotency_key: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"http_status": response.http_status, "body": response.body}
    if idempotency_key is not None:
        payload["idempotency_key"] = idempotency_key
    if not response.succeeded():
        raise ToolError(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return payload


def _error_payload(code: str, idempotency_key: str | None) -> str:
    payload: dict[str, str] = {"error": code}
    if idempotency_key is not None:
        payload["idempotency_key"] = idempotency_key
    return json.dumps(payload, sort_keys=True)


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise ToolError("invalid_id") from None
