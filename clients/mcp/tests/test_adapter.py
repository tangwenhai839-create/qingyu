"""The adapter calls the existing HTTP routes and reuses request ids."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import traceback
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import qingyu_mcp.gateway as gateway_module
import qingyu_mcp.pending as pending_module
import qingyu_mcp.server as server_module
from qingyu_mcp.gateway import GatewayAdapter, GatewayCallError

from tests.helpers import INGEST, READ, make_settings

OBS = "00000000-0000-4000-8000-000000000001"
ATT = "00000000-0000-4000-8000-000000000008"
OPAQUE = 'null+/=&%{"kind":"keep"}'


def test_adapter_does_not_hash_payloads() -> None:
    for module in (gateway_module, pending_module, server_module):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name != "hashlib" for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                assert node.module != "hashlib"


def test_search_and_read_pass_gateway_json_through(tmp_path: Path) -> None:
    search_page = {
        "contract_version": "evidence-search-v1",
        "meaning": "gateway_recorded_historical_evidence",
        "consistency": "keyword_substring_match_v1",
        "items": [{"snippet": "运维", "score": 1, "match_mode": "bigram", "future": True}],
        "next_cursor": "opaque-search",
        "limit": 20,
    }
    query_page = {
        "contract_version": "evidence-query-v1",
        "meaning": "gateway_recorded_historical_evidence",
        "consistency": "bounded_high_water_enumeration_v1",
        "items": [{"observed_title": "kept", "future": 1}],
        "next_cursor": None,
        "limit": 5,
    }
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/v1/evidence/search":
            return httpx.Response(200, json=search_page)
        return httpx.Response(200, json=query_page)

    adapter = GatewayAdapter(make_settings(tmp_path), transport=httpx.MockTransport(handler))
    try:
        search = adapter.search_evidence(
            {"q": "运维", "limit": 20, "from": "2026-01-01T00:00:00Z"}
        )
        observations = adapter.read_evidence(
            "/api/v1/evidence/conversation-observations",
            {"limit": 5, "cursor": OPAQUE},
        )
        nodes = adapter.read_evidence(
            f"/api/v1/evidence/conversation-observations/{OBS}/nodes"
        )
        attachments = adapter.read_evidence(
            f"/api/v1/evidence/conversation-observations/{OBS}/attachments/{ATT}/observations"
        )
    finally:
        adapter.close()

    assert search.http_status == 200
    assert search.body == search_page
    assert observations.body == query_page
    assert nodes.body["contract_version"] == "evidence-query-v1"
    assert attachments.body["items"][0]["future"] == 1
    assert all(request.headers["authorization"] == f"Bearer {READ}" for request in seen)
    assert all(INGEST not in request.headers["authorization"] for request in seen)
    query = parse_qs(urlsplit(str(seen[1].url)).query)
    assert query["cursor"] == [OPAQUE]
    assert query["limit"] == ["5"]
    assert not (tmp_path / "pending.jsonl").exists()


def test_disallowed_path_makes_no_request(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={})

    adapter = GatewayAdapter(make_settings(tmp_path), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(GatewayCallError, match="path_not_allowed"):
            adapter.read_evidence("/api/v1/ingest/messages:batch")
        with pytest.raises(GatewayCallError, match="path_not_allowed"):
            adapter.read_evidence("/api/v1/import/chatgpt")
    finally:
        adapter.close()
    assert seen == []


def test_redirect_is_not_followed(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://evil.example/collect"})

    adapter = GatewayAdapter(make_settings(tmp_path), transport=httpx.MockTransport(handler))
    try:
        result = adapter.search_evidence({"q": "运维"})
    finally:
        adapter.close()
    assert result.http_status == 302
    assert len(seen) == 1


def test_request_id_is_reused_after_the_adapter_is_reconstructed(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["client_request_id"])
        assert request.headers["authorization"] == f"Bearer {INGEST}"
        assert "payload_hash" not in body
        assert body["client_request_id"] != hashlib.sha256(request.content).hexdigest()
        if len(calls) == 1:
            raise httpx.ConnectError("down")
        return httpx.Response(
            202,
            json={"status": "processing", "client_request_id": body["client_request_id"]},
        )

    settings = make_settings(tmp_path)
    message = {
        "client_message_id": "msg-1",
        "role": "user",
        "content_text": "remember the backup window",
        "content_kind": "conversation_message",
    }
    first = GatewayAdapter(settings, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(GatewayCallError, match="gateway_unreachable"):
            first.ingest_message(
                idempotency_key="op-1",
                source_platform="cursor",
                source_conversation_id="conv-1",
                title=None,
                message=message,
            )
    finally:
        first.close()

    second = GatewayAdapter(settings, transport=httpx.MockTransport(handler))
    try:
        again = second.ingest_message(
            idempotency_key="op-1",
            source_platform="cursor",
            source_conversation_id="conv-1",
            title=None,
            message=message,
        )
        changed = second.ingest_message(
            idempotency_key="op-1",
            source_platform="cursor",
            source_conversation_id="conv-1",
            title=None,
            message={**message, "content_text": "a different body"},
        )
        other = second.ingest_conversation(
            idempotency_key="op-2",
            source_platform="cursor",
            source_conversation_id="conv-1",
            title="Notes",
        )
    finally:
        second.close()

    assert calls[0] == calls[1] == calls[2]
    assert calls[0].startswith("mcp-")
    assert calls[3] != calls[0]
    assert again.http_status == 202
    assert again.body["client_request_id"] == calls[0]
    assert changed.http_status == 202
    text = (tmp_path / "pending.jsonl").read_text(encoding="utf-8")
    assert READ not in text
    assert INGEST not in text
    assert other.body["client_request_id"] == calls[3]
    assert json.loads(
        [
            line
            for line in text.splitlines()
            if "op-2" in line
        ][0]
    )["kind"] == "conversation"


def test_unreachable_error_hides_the_token(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed {request.headers}")

    adapter = GatewayAdapter(make_settings(tmp_path), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(GatewayCallError) as caught:
            adapter.ingest_conversation(
                idempotency_key="op-1",
                source_platform="cursor",
                source_conversation_id="conv-1",
                title=None,
            )
    finally:
        adapter.close()
    error = caught.value
    assert str(error) == "gateway_unreachable"
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    assert READ not in rendered
    assert INGEST not in rendered


def test_response_body_is_redacted(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"echo": READ, "contract_version": "evidence-search-v1"})

    adapter = GatewayAdapter(make_settings(tmp_path), transport=httpx.MockTransport(handler))
    try:
        result = adapter.search_evidence({"q": "运维"})
    finally:
        adapter.close()
    encoded = json.dumps(result.body)
    assert READ not in encoded
    assert result.body["echo"] == "[redacted]"
    assert result.body["contract_version"] == "evidence-search-v1"
