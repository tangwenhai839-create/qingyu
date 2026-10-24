"""Request ids, access logs, and a per-process Prometheus registry.

Access lines contain the method, path, status, duration, request id, and the
direct peer. They do not contain the query string, headers, tokens, or bodies.
``X-Request-ID`` is echoed when it is a single short token; anything else is
replaced so a client cannot inject a line break into the log.

``/metrics`` is registered on the app and returns 404 until ``METRICS_ENABLED``
is true. ``METRICS_TOKEN``, when set, is required as a bearer token. The API
token is not accepted in its place. Counters are process-local.
"""

from __future__ import annotations

import contextvars
import logging
import re
import secrets
import threading
import time
import uuid

from fastapi import HTTPException

from .config import get_settings


_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "qingyu_request_id", default=None
)
access_logger = logging.getLogger("qingyu.access")


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id() or "-"
        return True


def current_request_id() -> str | None:
    return _request_id.get()


def bind_request_id(value: str) -> contextvars.Token:
    return _request_id.set(value)


def reset_request_id(token: contextvars.Token) -> None:
    _request_id.reset(token)


def resolve_request_id(scope) -> str:
    """Accept one well-formed ``X-Request-ID``. Otherwise generate one."""
    found: list[str] = []
    for name, value in scope.get("headers") or ():
        if name.lower() != b"x-request-id":
            continue
        found.append(value.decode("latin-1").strip())
    if len(found) == 1 and _REQUEST_ID.fullmatch(found[0]):
        return found[0]
    return uuid.uuid4().hex


class MetricsRegistry:
    """Request count and latency histogram labeled by route template and status."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[tuple[str, str], int] = {}
        self._buckets: dict[tuple[str, str], list[int]] = {}
        self._sums: dict[tuple[str, str], float] = {}
        self._observations: dict[tuple[str, str], int] = {}

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()
            self._buckets.clear()
            self._sums.clear()
            self._observations.clear()

    def observe(self, route: str, status: int, duration_seconds: float) -> None:
        key = (route, str(status))
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1
            buckets = self._buckets.setdefault(key, [0] * len(_BUCKETS))
            for index, bound in enumerate(_BUCKETS):
                if duration_seconds <= bound:
                    buckets[index] += 1
            self._sums[key] = self._sums.get(key, 0.0) + duration_seconds
            self._observations[key] = self._observations.get(key, 0) + 1

    def render(self) -> str:
        with self._lock:
            counts = dict(self._counts)
            buckets = {key: list(value) for key, value in self._buckets.items()}
            sums = dict(self._sums)
            observations = dict(self._observations)
        lines = [
            "# HELP qingyu_http_requests_total HTTP requests handled by this process.",
            "# TYPE qingyu_http_requests_total counter",
        ]
        for route, status in sorted(counts):
            lines.append(
                f"qingyu_http_requests_total{{{_labels(route, status)}}} {counts[(route, status)]}"
            )
        lines.extend(
            [
                "# HELP qingyu_http_request_duration_seconds HTTP request latency in seconds.",
                "# TYPE qingyu_http_request_duration_seconds histogram",
            ]
        )
        for route, status in sorted(buckets):
            labels = _labels(route, status)
            seen = buckets[(route, status)]
            for bound, count in zip(_BUCKETS, seen, strict=True):
                lines.append(
                    "qingyu_http_request_duration_seconds_bucket{"
                    f"{labels},le=\"{_bound(bound)}\"}} {count}"
                )
            lines.append(
                f"qingyu_http_request_duration_seconds_bucket{{{labels},le=\"+Inf\"}} {observations[(route, status)]}"
            )
            lines.append(
                f"qingyu_http_request_duration_seconds_sum{{{labels}}} {sums[(route, status)]:.6f}"
            )
            lines.append(
                f"qingyu_http_request_duration_seconds_count{{{labels}}} {observations[(route, status)]}"
            )
        return "\n".join(lines) + "\n"


def authorize_metrics(authorization: str | None) -> None:
    settings = get_settings()
    if not settings.metrics_enabled:
        raise HTTPException(status_code=404, detail="Not Found")
    expected = settings.metrics_token
    if not expected:
        return
    supplied = _bearer(authorization)
    if not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail={"code": "authentication_required"})


def _bearer(authorization: str | None) -> str:
    if not authorization:
        return ""
    scheme, _, rest = authorization.partition(" ")
    if scheme.lower() != "bearer" or not rest or " " in rest.strip():
        return ""
    return rest.strip()


class ObservabilityMiddleware:
    """Assign a request id, then log and measure the response. Does not read the body."""

    def __init__(self, app, metrics: MetricsRegistry):
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        request_id = resolve_request_id(scope)
        token = bind_request_id(request_id)
        started = time.perf_counter()
        status = 500
        try:
            async def send_with_id(message):
                nonlocal status
                if message["type"] == "http.response.start":
                    status = int(message["status"])
                    headers = [
                        (name, value)
                        for name, value in message.get("headers") or []
                        if name.lower() != b"x-request-id"
                    ]
                    headers.append((b"x-request-id", request_id.encode("ascii")))
                    message = {**message, "headers": headers}
                await send(message)

            await self.app(scope, receive, send_with_id)
        finally:
            duration = time.perf_counter() - started
            try:
                _log_access(scope, status, duration, request_id)
                if get_settings().metrics_enabled:
                    self.metrics.observe(_route_label(scope), status, duration)
            finally:
                reset_request_id(token)


def _log_access(scope, status: int, duration: float, request_id: str) -> None:
    client = scope.get("client")
    peer = client[0] if client else "-"
    access_logger.info(
        "method=%s path=%s status=%s duration_ms=%.3f client=%s",
        _field(str(scope.get("method") or "-")),
        _field(str(scope.get("path") or "-")),
        status,
        duration * 1000,
        _field(str(peer)),
        extra={"request_id": request_id},
    )


def _route_label(scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    return "unmatched"


def _field(value: str, limit: int = 200) -> str:
    return value.replace("\r", "").replace("\n", "")[:limit]


def _labels(route: str, status: str) -> str:
    return f'route="{_escape(route)}",status="{_escape(status)}"'


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _bound(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text or "0"
