"""Rotatable bearer tokens, request-body caps, and the fixed-window rate limit."""

import asyncio
import json
import secrets
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.auth import token_matches
from app.config import Settings, get_settings
from app.main import app
from app.request_limits import (
    MULTIPART_OVERHEAD_BYTES,
    FixedWindowRateLimiter,
    ImportRequestSizeLimitMiddleware,
    RateLimitMiddleware,
)

API_TOKEN = "qingyu-test-token-only"
READ_TOKEN = "qingyu-test-evidence-read-token-only"
API_HEADERS = {"Authorization": f"Bearer {API_TOKEN}"}
EVIDENCE_PATH = "/api/v1/evidence/conversation-observations"
PING_PATH = "/api/v1/protected-ping"


def _production(**overrides) -> Settings:
    values = {
        "app_env": "production",
        "api_token": "admin-production-secret",
        "api_tokens": "",
        "evidence_read_api_token": "read-production-secret",
        "evidence_read_api_tokens": "",
        "evidence_cursor_signing_key": "cursor-production-secret",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _arm_limiter(monkeypatch, *, requests: int, window: float, now: float):
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_requests", requests)
    monkeypatch.setattr(settings, "rate_limit_window_seconds", window)
    limiter = app.state.rate_limiter
    previous = limiter.clock
    limiter.clock = lambda: now
    limiter.reconfigure(requests, window)
    limiter.clear()
    return previous


def _restore_limiter(previous) -> None:
    app.state.rate_limiter.clock = previous
    app.state.rate_limiter.clear()


def _run_asgi(app_or_middleware, scope, receive):
    sent = []

    async def send(message):
        sent.append(message)

    asyncio.run(app_or_middleware(scope, receive, send))
    status = next(message["status"] for message in sent if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"") for message in sent if message["type"] == "http.response.body"
    )
    headers = {}
    for message in sent:
        if message["type"] != "http.response.start":
            continue
        for name, value in message.get("headers", []):
            headers[name.decode().lower()] = value.decode()
    return status, body, headers


def test_production_accepts_additional_tokens_and_rejects_bad_lists():
    settings = _production(
        api_tokens="  second-admin-secret , third-admin-secret ",
        evidence_read_api_tokens="second-read-secret",
    )

    assert settings.active_api_tokens() == (
        "admin-production-secret",
        "second-admin-secret",
        "third-admin-secret",
    )
    assert settings.active_evidence_read_tokens() == (
        "read-production-secret",
        "second-read-secret",
    )
    assert _production(api_tokens="   ").active_api_tokens() == ("admin-production-secret",)

    with pytest.raises(ValueError, match="token list contains an empty entry"):
        _production(api_tokens="second-admin-secret,")
    with pytest.raises(ValueError, match="token list contains an empty entry"):
        _production(evidence_read_api_tokens=",second-read-secret")
    with pytest.raises(ValueError, match="API_TOKENS contains a duplicate token"):
        _production(api_tokens="admin-production-secret")
    with pytest.raises(ValueError, match="API_TOKENS contains a duplicate token"):
        _production(api_tokens="second-admin-secret,second-admin-secret")
    with pytest.raises(ValueError, match="EVIDENCE_READ_API_TOKENS contains a duplicate token"):
        _production(evidence_read_api_tokens="read-production-secret")
    with pytest.raises(ValueError, match="evidence-read tokens must differ from API tokens"):
        _production(evidence_read_api_tokens="admin-production-secret")
    with pytest.raises(ValueError, match="evidence-read tokens must differ from API tokens"):
        _production(api_tokens="second-admin-secret", evidence_read_api_tokens="second-admin-secret")
    with pytest.raises(ValueError, match="must differ from every access token"):
        _production(api_tokens="cursor-production-secret")
    with pytest.raises(ValueError, match="must differ from every access token"):
        _production(evidence_read_api_tokens="cursor-production-secret")
    with pytest.raises(ValueError, match="API_TOKENS must be a non-example secret"):
        _production(api_tokens="changeme")
    with pytest.raises(ValueError, match="EVIDENCE_READ_API_TOKENS must be a non-example secret"):
        _production(evidence_read_api_tokens="Example")
    with pytest.raises(ValueError, match="API_TOKEN must be a non-example secret"):
        _production(api_token="", api_tokens="second-admin-secret")


def test_test_env_skips_token_list_checks_and_limits_have_defaults(monkeypatch):
    settings = Settings(
        app_env="test",
        api_token="changeme",
        api_tokens="changeme,",
        evidence_read_api_token="changeme",
        evidence_cursor_signing_key="changeme",
        _env_file=None,
    )
    assert settings.api_token == "changeme"
    with pytest.raises(ValueError, match="token list contains an empty entry"):
        settings.active_api_tokens()

    monkeypatch.delenv("RATE_LIMIT_ENABLED", raising=False)
    monkeypatch.delenv("RATE_LIMIT_REQUESTS", raising=False)
    monkeypatch.delenv("RATE_LIMIT_WINDOW_SECONDS", raising=False)
    monkeypatch.delenv("REQUEST_BODY_MAX_BYTES", raising=False)
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    disabled = Settings(app_env="test", _env_file=None)
    assert disabled.rate_limit_enabled is False
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    enabled = Settings(app_env="test", _env_file=None)
    assert enabled.rate_limit_enabled is True

    monkeypatch.delenv("RATE_LIMIT_ENABLED", raising=False)
    defaults = Settings(app_env="test", _env_file=None)
    assert defaults.rate_limit_enabled is True
    assert defaults.rate_limit_requests == 600
    assert defaults.rate_limit_window_seconds == 60
    assert defaults.request_body_max_bytes == 1024 * 1024

    with pytest.raises(ValidationError):
        Settings(app_env="test", rate_limit_requests=0, _env_file=None)
    with pytest.raises(ValidationError):
        Settings(app_env="test", rate_limit_window_seconds=0, _env_file=None)
    with pytest.raises(ValidationError):
        Settings(app_env="test", request_body_max_bytes=0, _env_file=None)


def test_api_tokens_overlap_and_then_the_old_token_stops(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "api_token", "new-api-token-value")
    monkeypatch.setattr(settings, "api_tokens", "  old-api-token-value ")
    client = TestClient(app)

    assert client.get(PING_PATH, headers=_bearer("new-api-token-value")).status_code == 200
    assert client.get(PING_PATH, headers=_bearer("old-api-token-value")).status_code == 200
    assert client.get(PING_PATH, headers=_bearer(READ_TOKEN)).status_code == 401
    assert token_matches("old-api-token-value ", ("old-api-token-value",)) is False

    monkeypatch.setattr(settings, "api_tokens", "")
    assert client.get(PING_PATH, headers=_bearer("old-api-token-value")).status_code == 401
    assert client.get(PING_PATH, headers=_bearer("new-api-token-value")).status_code == 200


def test_evidence_read_tokens_overlap_without_granting_mutation(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "evidence_read_api_token", "read-new-token")
    monkeypatch.setattr(settings, "evidence_read_api_tokens", "read-old-token")
    client = TestClient(app)

    assert client.get(EVIDENCE_PATH, headers=_bearer("read-new-token")).status_code == 200
    assert client.get(EVIDENCE_PATH, headers=_bearer("read-old-token")).status_code == 200
    assert client.get(EVIDENCE_PATH, headers=API_HEADERS).status_code == 200
    assert client.get(PING_PATH, headers=_bearer("read-new-token")).status_code == 401
    assert client.post("/api/v1/import/chatgpt", headers=_bearer("read-old-token")).status_code == 401
    assert client.post("/api/v1/archive/objects", headers=_bearer("read-new-token")).status_code == 401

    monkeypatch.setattr(settings, "evidence_read_api_tokens", "")
    removed = client.get(EVIDENCE_PATH, headers=_bearer("read-old-token"))
    assert removed.status_code == 401
    assert removed.json() == {"detail": {"code": "authentication_required"}}
    assert client.get(EVIDENCE_PATH, headers=_bearer("read-new-token")).status_code == 200


def test_compare_digest_runs_against_every_candidate(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "api_token", "alpha-token-value")
    monkeypatch.setattr(settings, "api_tokens", "beta-token-value,gamma-token-value")
    monkeypatch.setattr(settings, "evidence_read_api_token", "read-token-value")
    monkeypatch.setattr(settings, "evidence_read_api_tokens", "read-two-value")
    compared: list[tuple[str, str]] = []
    real_compare = secrets.compare_digest

    def spy(left, right):
        compared.append((left, right))
        return real_compare(left, right)

    monkeypatch.setattr("app.auth.secrets.compare_digest", spy)
    assert token_matches("alpha-token-value", ("alpha-token-value", "beta-token-value")) is True
    assert compared == [
        ("alpha-token-value", "alpha-token-value"),
        ("alpha-token-value", "beta-token-value"),
    ]
    assert token_matches("short", ("much-longer-token-value",)) is False

    compared.clear()
    client = TestClient(app)
    assert client.get(PING_PATH, headers=_bearer("alpha-token-value")).status_code == 200
    assert compared == [
        ("alpha-token-value", "alpha-token-value"),
        ("alpha-token-value", "beta-token-value"),
        ("alpha-token-value", "gamma-token-value"),
    ]

    compared.clear()
    assert client.get(PING_PATH, headers=_bearer("not-a-configured-token")).status_code == 401
    assert [right for _left, right in compared] == [
        "alpha-token-value",
        "beta-token-value",
        "gamma-token-value",
    ]

    compared.clear()
    assert client.get(PING_PATH).status_code == 401
    assert client.get(PING_PATH, headers={"Authorization": "Basic abc"}).status_code == 401
    assert client.get(EVIDENCE_PATH).status_code == 401
    assert compared == []

    compared.clear()
    assert client.get(EVIDENCE_PATH, headers=_bearer("alpha-token-value")).status_code == 200
    assert [right for _left, right in compared] == [
        "alpha-token-value",
        "beta-token-value",
        "gamma-token-value",
        "read-token-value",
        "read-two-value",
    ]

    compared.clear()
    assert client.get(EVIDENCE_PATH, headers=_bearer("read-token-value")).status_code == 200
    assert len(compared) == 5
    assert compared[-2][1] == "read-token-value"
    assert compared[-1] == ("read-token-value", "read-two-value")


def test_fixed_window_resets_is_per_key_and_prunes_expired_entries():
    clock = {"now": 100.0}
    limiter = FixedWindowRateLimiter(limit=2, window_seconds=10, clock=lambda: clock["now"])

    assert limiter.check("alpha") == (True, 0)
    assert limiter.check("alpha") == (True, 0)
    assert limiter.check("beta") == (True, 0)
    allowed, retry_after = limiter.check("alpha")
    assert allowed is False
    assert retry_after == 10
    clock["now"] = 109.9
    allowed, retry_after = limiter.check("alpha")
    assert allowed is False
    assert retry_after == 1
    clock["now"] = 110.0
    assert limiter.check("alpha") == (True, 0)
    assert limiter.check("alpha") == (True, 0)
    limiter.reconfigure(2, 10)
    assert limiter.check("alpha")[0] is False
    limiter.reconfigure(3, 10)
    assert limiter.check("alpha") == (True, 0)
    limiter.clear()
    assert limiter.check("alpha") == (True, 0)

    with pytest.raises(ValueError, match="positive"):
        FixedWindowRateLimiter(limit=0, window_seconds=10)
    with pytest.raises(ValueError, match="positive"):
        FixedWindowRateLimiter(limit=1, window_seconds=0)

    clock = {"now": 0.0}
    crowded = FixedWindowRateLimiter(limit=1, window_seconds=5, clock=lambda: clock["now"])
    for index in range(4096):
        assert crowded.check(f"key-{index}")[0] is True
    clock["now"] = 5.0
    assert crowded.check("fresh")[0] is True
    assert "key-0" not in crowded._windows
    assert "fresh" in crowded._windows


def test_fixed_window_count_is_exact_under_contention():
    limiter = FixedWindowRateLimiter(limit=40, window_seconds=60, clock=lambda: 0.0)
    results = []
    lock = threading.Lock()

    def worker():
        allowed, _retry_after = limiter.check("shared")
        with lock:
            results.append(allowed)

    threads = [threading.Thread(target=worker) for _ in range(100)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 100
    assert sum(results) == 40


def test_http_rate_limit_skips_health_and_ignores_forwarded_for(monkeypatch):
    previous = _arm_limiter(monkeypatch, requests=2, window=60, now=10_000.0)
    try:
        client = TestClient(app)
        for index in range(4):
            health = client.get("/health", headers={"X-Forwarded-For": f"203.0.113.{index}"})
            assert health.status_code == 200
        first = client.get(
            PING_PATH, headers={**API_HEADERS, "X-Forwarded-For": "198.51.100.10"}
        )
        second = client.get(
            PING_PATH, headers={**API_HEADERS, "X-Forwarded-For": "198.51.100.11"}
        )
        monkeypatch.setattr(get_settings(), "request_body_max_bytes", 8)
        third = client.post(
            PING_PATH,
            content=b"x" * 64,
            headers={**API_HEADERS, "X-Forwarded-For": "198.51.100.12"},
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert third.status_code == 429
        assert third.headers["retry-after"] == "60"
        assert third.json() == {"detail": {"code": "rate_limited"}}
        assert "qingyu-test-token-only" not in third.text
    finally:
        _restore_limiter(previous)


def test_disabled_rate_limit_does_not_consult_the_limiter(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    monkeypatch.setattr(settings, "rate_limit_requests", 1)

    def fail(*_args, **_kwargs):
        raise AssertionError("limiter consulted")

    monkeypatch.setattr(app.state.rate_limiter, "check", fail)
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    for _ in range(5):
        assert client.get(PING_PATH, headers=API_HEADERS).status_code == 200

    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    assert client.get("/health").status_code == 200
    with pytest.raises(AssertionError, match="limiter consulted"):
        client.get(PING_PATH, headers=API_HEADERS)


def test_rate_limit_keys_direct_client_address_and_does_not_read_when_limited(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_requests", 1)
    monkeypatch.setattr(settings, "rate_limit_window_seconds", 30)
    limiter = FixedWindowRateLimiter(limit=1, window_seconds=30, clock=lambda: 50.0)
    calls = []

    async def downstream(scope, _receive, send):
        calls.append(scope.get("client"))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def unread():
        raise AssertionError("body read")

    middleware = RateLimitMiddleware(downstream, limiter)

    def scope_for(host, port):
        return {
            "type": "http",
            "path": PING_PATH,
            "method": "POST",
            "client": (host, port),
            "headers": [(b"x-forwarded-for", b"203.0.113.5")],
        }

    first, _body, _headers = _run_asgi(middleware, scope_for("10.0.0.1", 1000), unread)
    second, body, headers = _run_asgi(middleware, scope_for("10.0.0.1", 2000), unread)
    third, _body, _headers = _run_asgi(middleware, scope_for("10.0.0.2", 1000), unread)
    missing, _body, _headers = _run_asgi(
        middleware,
        {"type": "http", "path": PING_PATH, "method": "GET", "headers": []},
        unread,
    )
    limited_missing, _body, _headers = _run_asgi(
        middleware,
        {"type": "http", "path": PING_PATH, "method": "GET", "headers": []},
        unread,
    )

    assert first == 204
    assert second == 429
    assert json.loads(body) == {"detail": {"code": "rate_limited"}}
    assert headers["retry-after"] == "30"
    assert third == 204
    assert missing == 204
    assert limited_missing == 429
    assert calls == [("10.0.0.1", 1000), ("10.0.0.2", 1000), None]

    reached = {"lifespan": False}

    async def lifespan_app(_scope, _receive, _send):
        reached["lifespan"] = True

    asyncio.run(
        RateLimitMiddleware(lifespan_app, limiter)({"type": "lifespan"}, unread, _async_send_unused)
    )
    assert reached["lifespan"] is True


async def _async_send_unused(_message):
    raise AssertionError("lifespan scope must not send an HTTP response")


def test_non_upload_body_cap_is_independent_of_the_upload_cap(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "request_body_max_bytes", 8)
    monkeypatch.setattr(settings, "import_upload_max_bytes", 1024)
    client = TestClient(app)
    allowed = client.post(PING_PATH, content=b"01234567", headers=API_HEADERS)
    rejected = client.post(PING_PATH, content=b"012345678", headers=API_HEADERS)
    upload = client.post(
        "/api/v1/import/chatgpt",
        headers=API_HEADERS,
        files={"file": ("notes.txt", b"0123456789", "text/plain")},
    )
    retry = client.post(
        f"/api/v1/import-jobs/{uuid.uuid4()}/retry",
        content=b"0123456789",
        headers=API_HEADERS,
    )
    small_retry = client.post(
        f"/api/v1/import-jobs/{uuid.uuid4()}/retry",
        content=b"{}",
        headers=API_HEADERS,
    )

    assert allowed.status_code == 405
    assert rejected.status_code == 413
    assert rejected.json()["detail"] == "请求体超过 8 字节限制"
    assert upload.status_code == 415
    assert retry.status_code == 413
    assert retry.json()["detail"] == "请求体超过 8 字节限制"
    assert small_retry.status_code == 404


def test_invalid_or_lying_content_length_is_rejected_before_the_route(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "request_body_max_bytes", 4)
    monkeypatch.setattr(settings, "import_upload_max_bytes", 4)
    route_called = False
    receive_calls = 0

    async def downstream(_scope, receive, send):
        nonlocal route_called
        route_called = True
        await receive()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        return {"type": "http.request", "body": b"12345", "more_body": False}

    middleware = ImportRequestSizeLimitMiddleware(downstream)

    def scope(path, method, headers):
        return {"type": "http", "method": method, "path": path, "headers": headers}

    status, body, _headers = _run_asgi(
        middleware,
        scope("/api/v1/protected-ping", "POST", [(b"content-length", b"5")]),
        receive,
    )
    assert status == 413
    assert json.loads(body)["detail"] == "请求体超过 4 字节限制"
    assert route_called is False
    assert receive_calls == 0

    route_called = False
    status, _body, _headers = _run_asgi(
        middleware,
        scope(
            "/api/v1/import/chatgpt",
            "POST",
            [(b"content-length", str(4 + MULTIPART_OVERHEAD_BYTES).encode())],
        ),
        receive,
    )
    assert status == 204
    assert route_called is True

    route_called = False
    receive_calls = 0
    cases = [
        [(b"content-length", b"-1")],
        [(b"content-length", b"nope")],
        [(b"content-length", b"1"), (b"Content-Length", b"1")],
        [(b"content-length", b"")],
    ]
    for headers in cases:
        route_called = False
        receive_calls = 0
        status, body, _headers = _run_asgi(
            middleware,
            scope("/api/v1/import/chatgpt", "POST", headers),
            receive,
        )
        assert status == 400
        assert json.loads(body) == {"detail": {"code": "invalid_content_length"}}
        assert route_called is False
        assert receive_calls == 0

    chunks = iter(
        [
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"5", "more_body": False},
        ]
    )

    async def chunked():
        return next(chunks)

    async def reading_downstream(_scope, receive, _send):
        await receive()
        await receive()

    status, body, _headers = _run_asgi(
        ImportRequestSizeLimitMiddleware(reading_downstream),
        scope("/api/v1/protected-ping", "POST", [(b"content-length", b"1")]),
        chunked,
    )
    assert status == 413
    assert json.loads(body)["detail"] == "请求体超过 4 字节限制"

    upload_chunks = iter(
        [
            {"type": "http.request", "body": b"x" * (4 + MULTIPART_OVERHEAD_BYTES), "more_body": True},
            {"type": "http.request", "body": b"x", "more_body": False},
        ]
    )

    async def upload_receive():
        return next(upload_chunks)

    status, body, _headers = _run_asgi(
        ImportRequestSizeLimitMiddleware(reading_downstream),
        scope("/api/v1/archive/objects", "POST", []),
        upload_receive,
    )
    assert status == 413
    assert json.loads(body)["detail"] == f"请求体超过 {4 + MULTIPART_OVERHEAD_BYTES} 字节限制"


def test_rate_limiter_clock_defaults_to_monotonic():
    limiter = FixedWindowRateLimiter(limit=1, window_seconds=60)
    assert limiter.clock == time.monotonic
