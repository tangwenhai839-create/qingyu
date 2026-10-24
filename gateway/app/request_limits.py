"""Request body caps and a per-process fixed-window rate limit.

Body limits:

- ``POST /api/v1/import/chatgpt`` and ``POST /api/v1/archive/objects`` keep
  ``IMPORT_UPLOAD_MAX_BYTES`` plus one mebibyte of multipart overhead. The
  streaming counter still applies when ``Content-Length`` is missing or smaller
  than the bytes that follow.
- Every other HTTP request is capped at ``REQUEST_BODY_MAX_BYTES`` (default
  1 MiB). A retry or other small route can no longer carry an unbounded body.
- A missing ``Content-Length`` is allowed and then counted as the body arrives.
  A negative, non-integer, or repeated ``Content-Length`` is rejected with 400
  before the application reads the body.

Rate limit:

- Fixed window, default 600 requests per 60 seconds, one bucket per direct
  client address. ``/health`` is exempt.
- The bucket is in-process. It is not shared across processes and it does not
  trust ``X-Forwarded-For``, which a client could spoof if the application
  port were reachable directly. The key is the direct client address, not the
  port. Expired windows are dropped once the map holds 4096 keys; live windows
  are kept.
- Disable with ``RATE_LIMIT_ENABLED=false``. The test suite does that so
  request volume is not part of the suite's result.
"""

from __future__ import annotations

import math
import threading
import time

from fastapi.responses import JSONResponse

from .config import get_settings

MULTIPART_OVERHEAD_BYTES = 1024 * 1024
UPLOAD_PATHS = frozenset({
    "/api/v1/import/chatgpt",
    "/api/v1/archive/objects",
})


class RequestBodyTooLarge(RuntimeError):
    pass


class InvalidContentLength(ValueError):
    pass


class ImportRequestSizeLimitMiddleware:
    """Bound request bodies before application code can spool them."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        path = scope.get("path")
        if scope.get("method") == "POST" and path in UPLOAD_PATHS:
            limit = settings.import_upload_max_bytes + MULTIPART_OVERHEAD_BYTES
        else:
            limit = settings.request_body_max_bytes
        try:
            content_length = _content_length(scope)
        except InvalidContentLength:
            await _send_invalid_length(scope, receive, send)
            return
        if content_length is not None and content_length > limit:
            await _send_too_large(scope, receive, send, limit)
            return

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except RequestBodyTooLarge:
            await _send_too_large(scope, receive, send, limit)


class FixedWindowRateLimiter:
    """Count requests per key inside a fixed window."""

    def __init__(self, *, limit: int, window_seconds: float, clock=None):
        if limit < 1:
            raise ValueError("rate limit must be positive")
        if window_seconds <= 0:
            raise ValueError("rate limit window must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self.clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._windows: dict[str, tuple[float, int]] = {}

    def clear(self) -> None:
        with self._lock:
            self._windows.clear()

    def reconfigure(self, limit: int, window_seconds: float) -> None:
        with self._lock:
            if limit == self.limit and window_seconds == self.window_seconds:
                return
            self._windows.clear()
            self.limit = limit
            self.window_seconds = window_seconds

    def check(self, key: str) -> tuple[bool, int]:
        """Return ``(allowed, retry_after_seconds)``. ``retry_after_seconds`` is 0 when allowed."""
        now = self.clock()
        with self._lock:
            self._prune(now)
            started, count = self._windows.get(key, (now, 0))
            if now - started >= self.window_seconds:
                started, count = now, 0
            if count >= self.limit:
                remaining = self.window_seconds - (now - started)
                retry_after = max(1, math.ceil(remaining))
                self._windows[key] = (started, count)
                return False, retry_after
            self._windows[key] = (started, count + 1)
            return True, 0

    def _prune(self, now: float) -> None:
        if len(self._windows) < 4096:
            return
        expired = [
            key
            for key, (started, _count) in self._windows.items()
            if now - started >= self.window_seconds
        ]
        for key in expired:
            del self._windows[key]


class RateLimitMiddleware:
    """Apply the process-local fixed window before the request is read."""

    def __init__(self, app, limiter: FixedWindowRateLimiter):
        self.app = app
        self.limiter = limiter

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return
        settings = get_settings()
        if not settings.rate_limit_enabled:
            await self.app(scope, receive, send)
            return
        self.limiter.reconfigure(settings.rate_limit_requests, settings.rate_limit_window_seconds)
        client = scope.get("client")
        host = client[0] if client else "unknown"
        allowed, retry_after = self.limiter.check(host)
        if allowed:
            await self.app(scope, receive, send)
            return
        response = JSONResponse(
            status_code=429,
            content={"detail": {"code": "rate_limited"}},
            headers={"Retry-After": str(retry_after)},
        )
        await response(scope, receive, send)


def _content_length(scope) -> int | None:
    found: list[int] = []
    for name, value in scope.get("headers", ()):
        if name.lower() != b"content-length":
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise InvalidContentLength from exc
        if parsed < 0:
            raise InvalidContentLength
        found.append(parsed)
    if not found:
        return None
    if len(found) != 1:
        raise InvalidContentLength
    return found[0]


async def _send_too_large(scope, receive, send, limit: int) -> None:
    response = JSONResponse(
        status_code=413,
        content={"detail": f"请求体超过 {limit} 字节限制"},
    )
    await response(scope, receive, send)


async def _send_invalid_length(scope, receive, send) -> None:
    response = JSONResponse(
        status_code=400,
        content={"detail": {"code": "invalid_content_length"}},
    )
    await response(scope, receive, send)
