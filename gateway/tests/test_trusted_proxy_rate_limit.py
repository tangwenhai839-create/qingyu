"""TRUSTED_PROXIES: the rate limit follows a proxy's client only when the peer is trusted."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.client_ip import client_ip, parse_trusted_proxies
from app.config import Settings, get_settings
from app.main import app
from app.request_limits import FixedWindowRateLimiter, RateLimitMiddleware

PING = "/api/v1/protected-ping"
API_TOKEN = "qingyu-test-token-only"
API_HEADERS = {"Authorization": f"Bearer {API_TOKEN}"}


def _production(**overrides) -> Settings:
    values = {
        "app_env": "production",
        "api_token": "admin-production-secret",
        "evidence_read_api_token": "read-production-secret",
        "evidence_cursor_signing_key": "cursor-production-secret",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


def _enable(monkeypatch, trusted_proxies: str) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_requests", 1)
    monkeypatch.setattr(settings, "rate_limit_window_seconds", 60)
    monkeypatch.setattr(settings, "trusted_proxies", trusted_proxies)


def _limiter(limit: int = 1) -> tuple[RateLimitMiddleware, list]:
    calls: list = []

    async def downstream(scope, _receive, send):
        calls.append(client_ip(scope, get_settings().trusted_proxies))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = RateLimitMiddleware(
        downstream,
        FixedWindowRateLimiter(limit=limit, window_seconds=60, clock=lambda: 1.0),
    )
    return middleware, calls


def _scope(host: str | None, headers: list[tuple[bytes, bytes]], *, port: int = 1000):
    scope = {
        "type": "http",
        "path": PING,
        "method": "GET",
        "headers": headers,
    }
    if host is not None:
        scope["client"] = (host, port)
    return scope


def _run(middleware, scope) -> int:
    sent = []

    async def receive():
        raise AssertionError("body read")

    async def send(message):
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))
    return next(message["status"] for message in sent if message["type"] == "http.response.start")


def _xff(*hops: str) -> list[tuple[bytes, bytes]]:
    return [(b"x-forwarded-for", ", ".join(hops).encode())]


def test_trusted_proxy_list_parses_and_rejects_bad_entries():
    assert parse_trusted_proxies("") == ()
    assert parse_trusted_proxies("   ") == ()
    networks = parse_trusted_proxies(" 10.0.0.0/8, 127.0.0.1, ::1/128 ")
    assert [str(network) for network in networks] == ["10.0.0.0/8", "127.0.0.1/32", "::1/128"]
    assert _production(trusted_proxies="192.168.0.0/16").trusted_proxies == "192.168.0.0/16"
    assert Settings(app_env="test", _env_file=None).trusted_proxies == ""

    with pytest.raises(ValidationError, match="empty entry"):
        _production(trusted_proxies="10.0.0.0/8,")
    with pytest.raises(ValidationError, match="not a valid CIDR"):
        _production(trusted_proxies="not-a-cidr")
    with pytest.raises(ValidationError, match="not a valid CIDR"):
        _production(trusted_proxies="10.1.2.3/8")
    with pytest.raises(ValidationError, match="not a valid CIDR"):
        Settings(app_env="test", trusted_proxies="example.com", _env_file=None)


def test_untrusted_peer_ignores_spoofed_forwarded_headers(monkeypatch):
    _enable(monkeypatch, "10.0.0.0/8")
    middleware, calls = _limiter()
    peer = "203.0.113.9"

    first = _run(middleware, _scope(peer, _xff("198.51.100.10")))
    second = _run(middleware, _scope(peer, _xff("198.51.100.11"), port=2000))
    third = _run(
        middleware,
        _scope(peer, [(b"x-real-ip", b"198.51.100.12"), (b"X-Forwarded-For", b"198.51.100.13")]),
    )
    other = _run(middleware, _scope("203.0.113.10", _xff("198.51.100.10")))

    assert [first, second, third, other] == [204, 429, 429, 204]
    assert calls == [peer, "203.0.113.10"]


def test_empty_trusted_proxies_ignores_forwarded_headers(monkeypatch):
    _enable(monkeypatch, "")
    middleware, calls = _limiter()

    assert _run(middleware, _scope("10.0.0.5", _xff("203.0.113.5"))) == 204
    assert _run(middleware, _scope("10.0.0.5", [(b"x-real-ip", b"203.0.113.6")])) == 429
    assert _run(middleware, _scope("10.0.0.6", _xff("203.0.113.5"))) == 204
    assert calls == ["10.0.0.5", "10.0.0.6"]


def test_trusted_peer_uses_rightmost_untrusted_hop_and_drops_spoofed_prefix(monkeypatch):
    _enable(monkeypatch, "10.0.0.0/8, 192.168.0.0/16")
    middleware, calls = _limiter()

    real = _run(
        middleware,
        _scope("10.0.0.2", _xff("203.0.113.1", "198.51.100.20", "192.168.1.5")),
    )
    same_client_spoofed_again = _run(
        middleware,
        _scope("10.0.0.3", _xff("8.8.8.8", "198.51.100.20", "192.168.1.9")),
    )
    different_client = _run(
        middleware,
        _scope("10.0.0.2", _xff("198.51.100.20", "198.51.100.21")),
    )
    outside = _run(middleware, _scope("11.0.0.1", _xff("198.51.100.21")))

    assert [real, same_client_spoofed_again, different_client, outside] == [204, 429, 204, 204]
    assert calls == ["198.51.100.20", "198.51.100.21", "11.0.0.1"]


def test_multi_hop_chain_skips_every_trusted_proxy(monkeypatch):
    _enable(monkeypatch, "10.0.0.0/8, 172.16.0.0/12, fd00::/8")
    middleware, calls = _limiter()

    status = _run(
        middleware,
        _scope("10.0.0.4", _xff("203.0.113.50", "172.16.0.8", "10.1.1.1")),
    )
    repeated = _run(
        middleware,
        _scope("fd00::1", _xff("203.0.113.50", "fd00::2")),
    )
    leftmost_when_every_hop_is_trusted = _run(
        middleware,
        _scope("10.0.0.4", _xff("10.9.9.9", "172.16.0.8")),
    )

    assert [status, repeated, leftmost_when_every_hop_is_trusted] == [204, 429, 204]
    assert calls == ["203.0.113.50", "10.9.9.9"]


def test_ipv6_trusted_untrusted_and_mapped_addresses(monkeypatch):
    _enable(monkeypatch, "fd00::/8, 10.0.0.0/8")
    middleware, calls = _limiter()

    trusted_v6 = _run(middleware, _scope("fd00::1", _xff("2001:db8::10", "fd00::2")))
    same_v6_different_text = _run(
        middleware,
        _scope("fd00::1", _xff("2001:0db8:0000::10")),
    )
    spoofed_from_untrusted_v6 = _run(
        middleware,
        _scope("2001:db8::50", _xff("fd00::1")),
    )
    mapped = _run(
        middleware,
        _scope("::ffff:10.0.0.1", _xff("::ffff:203.0.113.5")),
    )
    same_mapped_as_v4 = _run(middleware, _scope("10.0.0.8", _xff("203.0.113.5")))
    bracketed = _run(middleware, _scope("fd00::1", _xff("[2001:db8::11]")))

    assert [trusted_v6, same_v6_different_text, spoofed_from_untrusted_v6, mapped, same_mapped_as_v4, bracketed] == [
        204,
        429,
        204,
        204,
        429,
        204,
    ]
    assert calls == ["2001:db8::10", "2001:db8::50", "203.0.113.5", "2001:db8::11"]


def test_x_real_ip_is_used_only_when_the_peer_is_trusted_and_forwarded_for_is_absent(monkeypatch):
    _enable(monkeypatch, "127.0.0.1/32, ::1/128")
    middleware, calls = _limiter()

    via_real = _run(middleware, _scope("127.0.0.1", [(b"x-real-ip", b"203.0.113.8")]))
    same_real = _run(middleware, _scope("::1", [(b"X-Real-IP", b"203.0.113.8")]))
    forwarded_wins = _run(
        middleware,
        _scope(
            "127.0.0.1",
            [(b"x-real-ip", b"198.51.100.1"), (b"x-forwarded-for", b"203.0.113.9")],
        ),
    )
    untrusted_real = _run(middleware, _scope("203.0.113.4", [(b"x-real-ip", b"198.51.100.9")]))
    repeated_real_is_ignored = _run(
        middleware,
        _scope(
            "127.0.0.1",
            [(b"x-real-ip", b"203.0.113.20"), (b"x-real-ip", b"203.0.113.21")],
        ),
    )

    assert [via_real, same_real, forwarded_wins, untrusted_real, repeated_real_is_ignored] == [
        204,
        429,
        204,
        204,
        204,
    ]
    assert calls == ["203.0.113.8", "203.0.113.9", "203.0.113.4", "127.0.0.1"]


def test_unparseable_hop_cannot_mint_buckets(monkeypatch):
    _enable(monkeypatch, "10.0.0.0/8")
    middleware, calls = _limiter()

    first = _run(middleware, _scope("10.0.0.2", _xff("203.0.113.5", "not-an-ip")))
    second = _run(middleware, _scope("10.0.0.2", _xff("another", "203.0.113.5:443")))
    third = _run(middleware, _scope("10.0.0.2", _xff("203.0.113.7", "10.0.0.9")))

    assert [first, second, third] == [204, 429, 204]
    assert calls == ["10.0.0.2", "203.0.113.7"]


def test_separate_forwarded_for_headers_are_one_chain(monkeypatch):
    _enable(monkeypatch, "10.0.0.0/8")
    scope = _scope(
        "10.0.0.1",
        [(b"x-forwarded-for", b"203.0.113.5"), (b"x-forwarded-for", b"10.0.0.9")],
    )
    assert client_ip(scope, "10.0.0.0/8") == "203.0.113.5"


def test_http_client_behind_a_trusted_proxy_does_not_share_one_bucket(monkeypatch):
    _enable(monkeypatch, "10.0.0.0/8")
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_requests", 1)
    monkeypatch.setattr(settings, "rate_limit_window_seconds", 60)
    previous = app.state.rate_limiter.clock
    app.state.rate_limiter.clock = lambda: 25_000.0
    app.state.rate_limiter.clear()
    try:
        proxy = TestClient(app, client=("10.0.0.8", 443))
        outsider = TestClient(app, client=("203.0.113.50", 443))
        first = proxy.get(PING, headers={**API_HEADERS, "X-Forwarded-For": "198.51.100.10"})
        second = proxy.get(PING, headers={**API_HEADERS, "X-Forwarded-For": "198.51.100.10"})
        other = proxy.get(PING, headers={**API_HEADERS, "X-Forwarded-For": "1.2.3.4, 198.51.100.11"})
        spoofed = outsider.get(
            PING,
            headers={**API_HEADERS, "X-Forwarded-For": "198.51.100.11", "X-Real-IP": "198.51.100.11"},
        )
        assert first.status_code == 200
        assert second.status_code == 429
        assert second.json() == {"detail": {"code": "rate_limited"}}
        assert other.status_code == 200
        assert spoofed.status_code == 200
        again = outsider.get(PING, headers={**API_HEADERS, "X-Forwarded-For": "198.51.100.99"})
        assert again.status_code == 429
        assert "qingyu-test-token-only" not in again.text
    finally:
        app.state.rate_limiter.clock = previous
        app.state.rate_limiter.clear()
        monkeypatch.setattr(get_settings(), "rate_limit_enabled", False)
