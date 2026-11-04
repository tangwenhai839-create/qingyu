"""HTTP calls to the existing gateway routes.

Reads use the evidence-read token. Writes use the ingest token and only the
two native ingest endpoints. The pending log is consulted before a write so
a retry, including one after this process is gone, sends the same
client_request_id. This module does not compute a payload hash.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from typing import Any

import httpx

from .config import Settings, redact
from .pending import PendingLog

log = logging.getLogger("qingyu_mcp")

_SEARCH = "/api/v1/evidence/search"
_INGEST_MESSAGE = "/api/v1/ingest/messages"
_INGEST_CONVERSATION = "/api/v1/ingest/conversations"
_OBSERVATIONS = "/api/v1/evidence/conversation-observations"
_UUID = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_NODES = re.compile(rf"^{_OBSERVATIONS}/{_UUID}/nodes$")
_ATTACHMENTS = re.compile(
    rf"^{_OBSERVATIONS}/{_UUID}/attachments/{_UUID}/observations$"
)
_SUCCESS = frozenset({200, 202})


class GatewayCallError(Exception):
    """The gateway was not reached, or the path is not one of the four calls."""


class GatewayResponse:
    def __init__(self, http_status: int, body: dict[str, Any]) -> None:
        self.http_status = http_status
        self.body = body

    def succeeded(self) -> bool:
        return self.http_status in _SUCCESS


class GatewayAdapter:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._pending = PendingLog(settings.pending_path)
        self._http = httpx.Client(
            base_url=settings.base_url,
            timeout=settings.timeout_seconds,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._http.close()

    def search_evidence(self, body: Mapping[str, Any]) -> GatewayResponse:
        return self._send("POST", _SEARCH, token=self._settings.read_token, json_body=dict(body))

    def read_evidence(
        self,
        path: str,
        params: Mapping[str, str | int] | None = None,
    ) -> GatewayResponse:
        return self._send(
            "GET",
            path,
            token=self._settings.read_token,
            params=dict(params) if params else None,
        )

    def ingest_message(
        self,
        *,
        idempotency_key: str,
        source_platform: str,
        source_conversation_id: str,
        title: str | None,
        message: Mapping[str, Any],
    ) -> GatewayResponse:
        request_id = self._pending.assign(idempotency_key, kind="message")
        body: dict[str, Any] = {
            "client_request_id": request_id,
            "source_platform": source_platform,
            "source_conversation_id": source_conversation_id,
            "message": dict(message),
        }
        if title is not None:
            body["title"] = title
        return self._send(
            "POST",
            _INGEST_MESSAGE,
            token=self._settings.ingest_token,
            json_body=body,
        )

    def ingest_conversation(
        self,
        *,
        idempotency_key: str,
        source_platform: str,
        source_conversation_id: str,
        title: str | None,
    ) -> GatewayResponse:
        request_id = self._pending.assign(idempotency_key, kind="conversation")
        body: dict[str, Any] = {
            "client_request_id": request_id,
            "source_platform": source_platform,
            "source_conversation_id": source_conversation_id,
        }
        if title is not None:
            body["title"] = title
        return self._send(
            "POST",
            _INGEST_CONVERSATION,
            token=self._settings.ingest_token,
            json_body=body,
        )

    def _send(
        self,
        method: str,
        path: str,
        *,
        token: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str | int] | None = None,
    ) -> GatewayResponse:
        if not _allowed(method, path):
            raise GatewayCallError("path_not_allowed")
        try:
            response = self._http.request(
                method,
                path,
                headers={"Authorization": f"Bearer {token}"},
                json=json_body,
                params=params,
            )
        except httpx.HTTPError:
            log.info("gateway request failed method=%s path=%s", method, path)
            raise GatewayCallError("gateway_unreachable") from None
        if response.status_code not in _SUCCESS:
            log.info(
                "gateway status=%s method=%s path=%s",
                response.status_code,
                method,
                path,
            )
        return GatewayResponse(response.status_code, _body(response, self._settings.secret_values()))


def _allowed(method: str, path: str) -> bool:
    if method == "POST" and path in {_SEARCH, _INGEST_MESSAGE, _INGEST_CONVERSATION}:
        return True
    if method == "GET" and (
        path == _OBSERVATIONS or _NODES.fullmatch(path) is not None or _ATTACHMENTS.fullmatch(path) is not None
    ):
        return True
    return False


def _body(response: httpx.Response, secrets: tuple[str, str]) -> dict[str, Any]:
    text = redact(response.text, secrets)
    if not text.strip():
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}
    if isinstance(parsed, dict):
        return parsed
    return {"value": parsed}


__all__ = [
    "GatewayAdapter",
    "GatewayCallError",
    "GatewayResponse",
]
