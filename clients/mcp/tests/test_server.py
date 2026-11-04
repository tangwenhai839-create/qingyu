"""The four tools are the MCP surface. stdio is the only transport main starts."""

from __future__ import annotations

import ast
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from qingyu_mcp.config import ENV_BASE_URL, ENV_INGEST_TOKEN, ENV_PENDING_PATH, ENV_READ_TOKEN
from qingyu_mcp.gateway import GatewayAdapter
from qingyu_mcp.server import TOOL_NAMES, build_server

from tests.helpers import INGEST, READ, make_settings

MESSAGE = {
    "idempotency_key": "op-restart-1",
    "source_platform": "cursor",
    "source_conversation_id": "conv-1",
    "client_message_id": "msg-1",
    "role": "user",
    "content_text": "remember the backup window",
    "content_kind": "conversation_message",
}


def test_main_runs_stdio_only() -> None:
    tree = ast.parse(inspect.getsource(sys.modules["qingyu_mcp.server"]))
    transports: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == "transport" and isinstance(keyword.value, ast.Constant):
                transports.append(keyword.value.value)
    assert transports == ["stdio"]


def test_process_exits_when_configuration_is_missing(tmp_path: Path) -> None:
    env = os.environ.copy()
    for name in (ENV_BASE_URL, ENV_READ_TOKEN, ENV_INGEST_TOKEN, ENV_PENDING_PATH):
        env.pop(name, None)
    root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-m", "qingyu_mcp"],
        input=b"",
        capture_output=True,
        cwd=tmp_path,
        env=env,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 2
    stderr = completed.stderr.decode("utf-8")
    assert ENV_BASE_URL in stderr
    assert READ not in stderr
    assert INGEST not in stderr


@pytest.mark.anyio
async def test_tool_list_is_the_four_gateway_tools(tmp_path: Path) -> None:
    adapter = GatewayAdapter(
        make_settings(tmp_path),
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
    )
    try:
        async with Client(build_server(adapter), raise_exceptions=True) as client:
            listed = await client.list_tools()
    finally:
        adapter.close()
    assert sorted(tool.name for tool in listed.tools) == sorted(TOOL_NAMES)
    for tool in listed.tools:
        props = tool.input_schema["properties"]
        assert "client_request_id" not in props
        assert "payload_hash" not in props
        assert tool.annotations is not None
        if tool.name.startswith("ingest_"):
            assert "idempotency_key" in tool.input_schema["required"]
            assert tool.annotations.read_only_hint is False
            assert tool.annotations.idempotent_hint is True
        else:
            assert tool.annotations.read_only_hint is True


@pytest.mark.anyio
async def test_search_tool_returns_the_search_contract(tmp_path: Path) -> None:
    page = {
        "contract_version": "evidence-search-v1",
        "meaning": "gateway_recorded_historical_evidence",
        "consistency": "keyword_substring_match_v1",
        "items": [{"snippet": "运维", "score": 1, "match_mode": "bigram"}],
        "next_cursor": None,
        "limit": 20,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/evidence/search"
        body = json.loads(request.content)
        assert body["q"] == "运维"
        assert body["from"] == "2026-01-01T00:00:00Z"
        assert "payload_hash" not in body
        assert request.headers["authorization"] == f"Bearer {READ}"
        return httpx.Response(200, json=page)

    adapter = GatewayAdapter(make_settings(tmp_path), transport=httpx.MockTransport(handler))
    try:
        async with Client(build_server(adapter), raise_exceptions=True) as client:
            result = await client.call_tool(
                "search_evidence",
                {"q": "运维", "time_from": "2026-01-01T00:00:00Z"},
            )
    finally:
        adapter.close()
    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["http_status"] == 200
    assert result.structured_content["body"] == page


@pytest.mark.anyio
async def test_read_tool_calls_the_nodes_route(tmp_path: Path) -> None:
    observation_id = "00000000-0000-4000-8000-000000000001"
    page = {
        "contract_version": "evidence-query-v1",
        "meaning": "gateway_recorded_historical_evidence",
        "consistency": "bounded_high_water_enumeration_v1",
        "items": [],
        "next_cursor": None,
        "limit": 100,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path.endswith(f"/{observation_id}/nodes")
        assert request.headers["authorization"] == f"Bearer {READ}"
        assert INGEST not in request.headers["authorization"]
        return httpx.Response(200, json=page)

    adapter = GatewayAdapter(make_settings(tmp_path), transport=httpx.MockTransport(handler))
    try:
        async with Client(build_server(adapter), raise_exceptions=True) as client:
            result = await client.call_tool(
                "read_evidence",
                {
                    "view": "nodes",
                    "conversation_observation_id": observation_id,
                },
            )
    finally:
        adapter.close()
    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["body"]["contract_version"] == "evidence-query-v1"


@pytest.mark.anyio
async def test_ingest_tool_reuses_the_request_id_across_servers(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body["client_request_id"])
        assert request.url.path == "/api/v1/ingest/messages"
        assert request.headers["authorization"] == f"Bearer {INGEST}"
        if len(seen) == 1:
            raise httpx.ConnectError("down")
        return httpx.Response(202, json={"status": "processing", "client_request_id": body["client_request_id"]})

    settings = make_settings(tmp_path)
    first = GatewayAdapter(settings, transport=httpx.MockTransport(handler))
    try:
        async with Client(build_server(first), raise_exceptions=True) as client:
            failed = await client.call_tool("ingest_message", MESSAGE)
    finally:
        first.close()
    assert failed.is_error is True
    assert "gateway_unreachable" in failed.content[0].text
    assert "op-restart-1" in failed.content[0].text

    second = GatewayAdapter(settings, transport=httpx.MockTransport(handler))
    try:
        async with Client(build_server(second), raise_exceptions=True) as client:
            retried = await client.call_tool("ingest_message", MESSAGE)
    finally:
        second.close()
    assert retried.is_error is False
    assert seen[0] == seen[1]
    assert seen[0].startswith("mcp-")
    assert retried.structured_content is not None
    assert retried.structured_content["body"]["client_request_id"] == seen[0]
    assert retried.structured_content["idempotency_key"] == "op-restart-1"


@pytest.mark.anyio
async def test_stdio_lists_the_tools(tmp_path: Path) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "qingyu_mcp"],
        env=_stdio_env(tmp_path),
        cwd=tmp_path,
    )
    async with Client(params, read_timeout_seconds=20) as client:
        listed = await client.list_tools()
    assert sorted(tool.name for tool in listed.tools) == sorted(TOOL_NAMES)


def test_stdio_logs_do_not_contain_tokens(tmp_path: Path) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "qingyu_mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
        env=_stdio_env(tmp_path),
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None
    frame = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "qingyu-mcp-test", "version": "0"},
            },
        }
    ).encode("utf-8") + b"\n"
    try:
        stdout, stderr = proc.communicate(frame, timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
    text_out = stdout.decode("utf-8", errors="replace")
    text_err = stderr.decode("utf-8", errors="replace")
    assert READ not in text_out
    assert INGEST not in text_out
    assert READ not in text_err
    assert INGEST not in text_err
    assert "stdio adapter ready" in text_err
    assert "search_evidence" in text_out


def _stdio_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env[ENV_BASE_URL] = "http://127.0.0.1:9"
    env[ENV_READ_TOKEN] = READ
    env[ENV_INGEST_TOKEN] = INGEST
    env[ENV_PENDING_PATH] = str(tmp_path / "pending.jsonl")
    return env
