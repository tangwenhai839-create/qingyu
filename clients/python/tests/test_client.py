"""Mocked-transport tests for the read-only Evidence Query client."""

from __future__ import annotations

import inspect
import uuid

import httpx
import pytest
from qingyu_client import QingyuClient

BASE = "https://gw.example.test"
OBS_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
CONV_ID = uuid.UUID("00000000-0000-4000-8000-000000000003")
SRC_ID = uuid.UUID("00000000-0000-4000-8000-000000000004")
NODE_ID = uuid.UUID("00000000-0000-4000-8000-000000000006")
MAP_NODE_ID = uuid.UUID("00000000-0000-4000-8000-000000000007")
ATT_ID = uuid.UUID("00000000-0000-4000-8000-000000000008")
ATT_OBS_ID = uuid.UUID("00000000-0000-4000-8000-000000000009")
OBSERVATIONS_PATH = "/api/v1/evidence/conversation-observations"
NODES_PATH = f"/api/v1/evidence/conversation-observations/{OBS_ID}/nodes"
ATTACHMENTS_PATH = (
    f"/api/v1/evidence/conversation-observations/{OBS_ID}"
    f"/attachments/{ATT_ID}/observations"
)
# Looks like JSON, includes characters that must survive query encoding,
# and is the string "null" rather than JSON null. The client must not
# interpret any of that.
OPAQUE = 'null+/=&%{"kind":"keep"}'
SECOND = "second page"


def _obs(title: str) -> dict:
    return {
        "conversation_observation_id": str(OBS_ID),
        "import_batch_commit_id": "00000000-0000-4000-8000-000000000002",
        "conversation_id": str(CONV_ID),
        "source_account_id": str(SRC_ID),
        "archive_object_id": "00000000-0000-4000-8000-000000000005",
        "source_platform": "chatgpt",
        "parser_name": "chatgpt",
        "parser_contract_version": "1",
        "committed_at": "2026-10-01T00:00:00Z",
        "observed_at": "2026-09-30T00:00:00Z",
        "observed_title": title,
        "normalized_created_at": None,
        "normalized_updated_at": None,
        "create_time_parse_status": "absent",
        "update_time_parse_status": "absent",
        "current_conversation_node_id": None,
        "current_node_status": "leaf",
        "source_observation_fingerprint": "ab" * 32,
        "canonical_fragment_fingerprint": "cd" * 32,
        "fragment_fingerprint_version": "v1",
    }


def _node(order: int) -> dict:
    return {
        "mapping_node_observation_id": str(MAP_NODE_ID),
        "conversation_node_id": str(NODE_ID),
        "parent_conversation_node_id": None,
        "declared_child_conversation_node_ids": None,
        "message_presence": "absent",
        "observation_order": order,
        "source_observation_fingerprint": "ab" * 32,
        "canonical_fragment_fingerprint": "cd" * 32,
        "fragment_fingerprint_version": "v1",
        "message": None,
    }


def _attachment() -> dict:
    return {
        "attachment_id": str(ATT_ID),
        "filename": "photo.png",
        "media_type": "image/png",
        "size_bytes": 12,
    }


def _attempt(number: int) -> dict:
    return {
        "attachment_observation_id": str(ATT_OBS_ID),
        "attempt_number": number,
        "availability": "available",
        "archive_object_id": None,
        "observed_filename": "photo.png",
        "observed_media_type": "image/png",
        "observed_size_bytes": 12,
        "resolution_status": None,
        "canonical_fragment_fingerprint": None,
        "fragment_fingerprint_version": None,
        "observed_at": "2026-10-01T00:00:00Z",
    }


def _page(items: list[dict], next_cursor: str | None, *, limit: int, kind: str) -> dict:
    body = {
        "contract_version": "evidence-query-v1",
        "meaning": "gateway_recorded_historical_evidence",
        "consistency": "bounded_high_water_enumeration_v1",
        "items": items,
        "next_cursor": next_cursor,
        "limit": limit,
    }
    if kind == "nodes":
        body["observation"] = _obs("A talk")
    elif kind == "attachments":
        body["attachment"] = _attachment()
    return body


def _client(handler, *, token: str = "tok", base_url: str = BASE + "/") -> QingyuClient:
    return QingyuClient(base_url, token, transport=httpx.MockTransport(handler))


def test_constructor_sends_bearer_and_uses_evidence_prefix():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["auth"] = request.headers["Authorization"]
        seen["url"] = str(request.url)
        seen["path"] = request.url.path
        return httpx.Response(200, json=_page([_obs("A talk")], None, limit=50, kind="observations"))

    with _client(handler, token="read-token") as client:
        # Trailing slash on the origin is stripped. Comparing without rstrip
        # would hide a constructor that left the slash in place.
        assert str(client._http.base_url) == BASE
        page = client.conversation_observations()

    assert seen["method"] == "GET"
    assert seen["auth"] == "Bearer read-token"
    assert seen["path"] == OBSERVATIONS_PATH
    assert seen["url"].startswith(BASE + OBSERVATIONS_PATH)
    assert "//api/" not in seen["url"].split("://", 1)[1]
    assert "read-token" not in seen["url"]
    assert page.items[0].observed_title == "A talk"
    assert page.next_cursor is None


def test_single_page_routes_filters_and_opaque_cursor():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        calls.append(request)
        kind = _kind(request.url.path)
        limit = int(request.url.params["limit"])
        return httpx.Response(200, json=_page([_item(kind, 1)], None, limit=limit, kind=kind))

    with _client(handler) as client:
        observations = client.conversation_observations(
            limit=2,
            cursor=OPAQUE,
            source_account_id=SRC_ID,
            conversation_id=CONV_ID,
        )
        nodes = client.nodes(OBS_ID, limit=3, cursor=OPAQUE)
        attachments = client.attachment_observations(OBS_ID, ATT_ID, limit=4, cursor=OPAQUE)

    assert observations.items[0].observed_title == "title-1"
    assert nodes.items[0].observation_order == 1
    assert attachments.items[0].attempt_number == 1
    assert [request.url.path for request in calls] == [
        OBSERVATIONS_PATH,
        NODES_PATH,
        ATTACHMENTS_PATH,
    ]
    first = calls[0].url.params
    assert first["cursor"] == OPAQUE
    assert first["limit"] == "2"
    assert first["source_account_id"] == str(SRC_ID)
    assert first["conversation_id"] == str(CONV_ID)
    assert calls[1].url.params["cursor"] == OPAQUE
    assert calls[1].url.params["limit"] == "3"
    assert "source_account_id" not in calls[1].url.params
    assert calls[2].url.params["cursor"] == OPAQUE
    assert calls[2].url.params["limit"] == "4"


def test_defaults_match_gateway_limits():
    limits: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert "cursor" not in request.url.params
        limits.append((request.url.path, request.url.params["limit"]))
        kind = _kind(request.url.path)
        return httpx.Response(
            200,
            json=_page([_item(kind, 1)], None, limit=int(request.url.params["limit"]), kind=kind),
        )

    with _client(handler) as client:
        client.conversation_observations()
        client.nodes(OBS_ID)
        client.attachment_observations(OBS_ID, ATT_ID)

    assert limits == [
        (OBSERVATIONS_PATH, "50"),
        (NODES_PATH, "100"),
        (ATTACHMENTS_PATH, "100"),
    ]


@pytest.mark.parametrize("kind", ["observations", "nodes", "attachments"])
def test_iter_forwards_opaque_cursor_and_stops_only_on_null(kind: str):
    """Empty pages do not stop the walk, and the string "null" is not JSON null."""
    calls: list[httpx.Request] = []
    pages = [
        _page([_item(kind, 1)], OPAQUE, limit=5, kind=kind),
        _page([], SECOND, limit=5, kind=kind),
        _page([_item(kind, 3)], None, limit=5, kind=kind),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        calls.append(request)
        return httpx.Response(200, json=pages[len(calls) - 1])

    with _client(handler) as client:
        if kind == "observations":
            yielded = list(
                client.iter_conversation_observations(
                    limit=5,
                    source_account_id=SRC_ID,
                    conversation_id=CONV_ID,
                )
            )
            markers = [item.observed_title for item in yielded]
            assert markers == ["title-1", "title-3"]
        elif kind == "nodes":
            yielded = list(client.iter_nodes(OBS_ID, limit=5))
            markers = [item.observation_order for item in yielded]
            assert markers == [1, 3]
        else:
            yielded = list(client.iter_attachment_observations(OBS_ID, ATT_ID, limit=5))
            markers = [item.attempt_number for item in yielded]
            assert markers == [1, 3]
    assert len(calls) == 3
    assert [request.url.params.get("cursor") for request in calls] == [None, OPAQUE, SECOND]
    assert [request.url.path for request in calls] == [_path(kind)] * 3
    for request in calls:
        assert request.url.params["limit"] == "5"
        if kind == "observations":
            assert request.url.params["source_account_id"] == str(SRC_ID)
            assert request.url.params["conversation_id"] == str(CONV_ID)
        else:
            assert "source_account_id" not in request.url.params
            assert "conversation_id" not in request.url.params


def test_transport_emits_only_get_for_every_endpoint_and_walker():
    methods: list[str] = []
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        methods.append(request.method)
        paths.append(request.url.path)
        kind = _kind(request.url.path)
        # One follow-up page for walkers, then stop. Single-page calls ignore the cursor.
        nxt = None if request.url.params.get("cursor") else OPAQUE
        limit = int(request.url.params["limit"])
        return httpx.Response(200, json=_page([_item(kind, 1)], nxt, limit=limit, kind=kind))

    with _client(handler) as client:
        client.conversation_observations()
        client.nodes(OBS_ID)
        client.attachment_observations(OBS_ID, ATT_ID)
        list(client.iter_conversation_observations())
        list(client.iter_nodes(OBS_ID))
        list(client.iter_attachment_observations(OBS_ID, ATT_ID))

    assert methods
    assert set(methods) == {"GET"}
    assert set(paths) == {OBSERVATIONS_PATH, NODES_PATH, ATTACHMENTS_PATH}
    # Each walker issues the first page plus the opaque follow-up.
    assert len(methods) == 9


def test_iterator_parameters_are_explicit():
    observations = inspect.signature(QingyuClient.iter_conversation_observations)
    assert list(observations.parameters) == [
        "self",
        "limit",
        "source_account_id",
        "conversation_id",
    ]
    nodes = inspect.signature(QingyuClient.iter_nodes)
    assert list(nodes.parameters) == ["self", "conversation_observation_id", "limit"]
    attachments = inspect.signature(QingyuClient.iter_attachment_observations)
    assert list(attachments.parameters) == [
        "self",
        "conversation_observation_id",
        "attachment_id",
        "limit",
    ]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for name, parameter in observations.parameters.items()
        if name != "self"
    )
    assert nodes.parameters["limit"].kind is inspect.Parameter.KEYWORD_ONLY
    assert attachments.parameters["limit"].kind is inspect.Parameter.KEYWORD_ONLY
    for signature in (observations, nodes, attachments):
        assert not any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        assert "cursor" not in signature.parameters

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected {request.method} {request.url}")

    with _client(handler) as client:
        with pytest.raises(TypeError):
            list(client.iter_conversation_observations(cursor=OPAQUE))
        with pytest.raises(TypeError):
            list(client.iter_nodes(OBS_ID, cursor=OPAQUE))
        with pytest.raises(TypeError):
            list(client.iter_attachment_observations(OBS_ID, ATT_ID, cursor=OPAQUE))


def test_public_surface_is_read_only():
    public = {name for name in dir(QingyuClient) if not name.startswith("_")}
    assert public == {
        "attachment_observations",
        "close",
        "conversation_observations",
        "iter_attachment_observations",
        "iter_conversation_observations",
        "iter_nodes",
        "nodes",
    }


def test_http_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(401, json={"detail": {"code": "authentication_required"}})

    with _client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            client.conversation_observations()
        with pytest.raises(httpx.HTTPStatusError):
            client.nodes(OBS_ID)
        with pytest.raises(httpx.HTTPStatusError):
            client.attachment_observations(OBS_ID, ATT_ID)


def _kind(path: str) -> str:
    if "/attachments/" in path:
        return "attachments"
    if path.endswith("/nodes"):
        return "nodes"
    return "observations"


def _path(kind: str) -> str:
    if kind == "observations":
        return OBSERVATIONS_PATH
    if kind == "nodes":
        return NODES_PATH
    return ATTACHMENTS_PATH


def _item(kind: str, marker: int) -> dict:
    if kind == "observations":
        return _obs(f"title-{marker}")
    if kind == "nodes":
        return _node(marker)
    return _attempt(marker)
