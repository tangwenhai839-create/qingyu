"""JSON logs, request ids, access logs, and the optional Prometheus endpoint."""

import json
import logging
import re
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings, get_settings, normalize_log_format
from app.db import get_db
from app.evidence.query import EvidenceQueryService
from app.logging_config import JsonFormatter, build_formatter, configure_logging
from app.main import app
from app.observability import MetricsRegistry, RequestIdFilter, bind_request_id, reset_request_id, resolve_request_id

API_HEADERS = {"Authorization": "Bearer qingyu-test-token-only"}
READ_HEADERS = {"Authorization": "Bearer qingyu-test-evidence-read-token-only"}
TOKEN = "SUPER_SECRET_TOKEN_456"
BODY = "secret-body-marker-not-for-logs"
QUERY = "secret-query-marker-not-for-logs"


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


def test_log_format_accepts_only_json_or_text():
    assert normalize_log_format(" JSON ") == "json"
    assert normalize_log_format("text") == "text"
    assert Settings(app_env="test", _env_file=None).log_format == "text"
    assert Settings(app_env="test", _env_file=None).metrics_enabled is False
    assert Settings(app_env="test", _env_file=None).metrics_token == ""
    assert _production(log_format="json", metrics_enabled=True, metrics_token="metrics-only").log_format == "json"
    with pytest.raises(ValidationError, match="LOG_FORMAT"):
        _production(log_format="yaml")
    with pytest.raises(ValueError, match="LOG_FORMAT"):
        normalize_log_format("")


def test_json_and_text_formatters_carry_the_request_id_and_not_a_token():
    record = logging.LogRecord("qingyu.access", logging.INFO, __file__, 1, "method=GET path=/health status=200", (), None)
    record.request_id = "trace-1"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["level"] == "INFO"
    assert payload["logger"] == "qingyu.access"
    assert payload["request_id"] == "trace-1"
    assert payload["message"] == "method=GET path=/health status=200"
    assert TOKEN not in JsonFormatter().format(record)

    text = build_formatter("text").format(record)
    assert "request_id=trace-1" in text
    assert "method=GET path=/health status=200" in text
    configure_logging("not-a-level", "text")
    assert logging.getLogger().level == logging.INFO
    configure_logging("DEBUG", "json")
    assert logging.getLogger().level == logging.DEBUG
    configure_logging("INFO", "text")


def test_request_id_filter_reads_the_context():
    token = bind_request_id("trace-ctx")
    try:
        record = logging.LogRecord("app", logging.INFO, __file__, 1, "hello", (), None)
        assert RequestIdFilter().filter(record) is True
        assert record.request_id == "trace-ctx"
    finally:
        reset_request_id(token)


def test_request_id_is_accepted_generated_or_replaced():
    assert resolve_request_id({"headers": [(b"x-request-id", b"trace_1.ok-2")]}) == "trace_1.ok-2"
    generated = resolve_request_id({"headers": []})
    assert re.fullmatch(r"[0-9a-f]{32}", generated)
    for headers in (
        [(b"x-request-id", b"bad id")],
        [(b"x-request-id", b"line\nbreak")],
        [(b"x-request-id", b"one"), (b"x-request-id", b"two")],
        [(b"x-request-id", b"")],
        [(b"x-request-id", b"x" * 129)],
    ):
        value = resolve_request_id({"headers": headers})
        assert re.fullmatch(r"[0-9a-f]{32}", value)
        assert "bad id" not in value and "\n" not in value


def test_http_echoes_request_id_on_success_and_on_rate_limit(monkeypatch):
    client = TestClient(app)
    generated = client.get("/health")
    assert re.fullmatch(r"[0-9a-f]{32}", generated.headers["x-request-id"])
    supplied = client.get("/health", headers={"X-Request-ID": "trace-1"})
    assert supplied.headers["x-request-id"] == "trace-1"
    rejected = client.get("/health", headers={"X-Request-ID": "bad id"})
    assert re.fullmatch(r"[0-9a-f]{32}", rejected.headers["x-request-id"])
    assert "bad id" not in rejected.headers["x-request-id"]

    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_requests", 1)
    monkeypatch.setattr(settings, "rate_limit_window_seconds", 60)
    previous = app.state.rate_limiter.clock
    app.state.rate_limiter.clock = lambda: 80_000.0
    app.state.rate_limiter.clear()
    try:
        first = client.get("/api/v1/protected-ping", headers={**API_HEADERS, "X-Request-ID": "limited-1"})
        second = client.get("/api/v1/protected-ping", headers={**API_HEADERS, "X-Request-ID": "limited-2"})
        assert first.status_code == 200
        assert first.headers["x-request-id"] == "limited-1"
        assert second.status_code == 429
        assert second.headers["x-request-id"] == "limited-2"
    finally:
        app.state.rate_limiter.clock = previous
        app.state.rate_limiter.clear()


def test_server_error_reuses_the_request_id_and_hides_the_exception(monkeypatch):
    def fail(*_args, **_kwargs):
        raise SQLAlchemyError("secret-db-detail")

    monkeypatch.setattr(EvidenceQueryService, "list_conversation_observations", fail)
    app.dependency_overrides[get_db] = lambda: object()
    try:
        response = TestClient(app, raise_server_exceptions=False).get(
            "/api/v1/evidence/conversation-observations",
            headers={**READ_HEADERS, "X-Request-ID": "trace-err-1"},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 500
    assert response.headers["x-request-id"] == "trace-err-1"
    assert response.json()["detail"]["request_id"] == "trace-err-1"
    assert "secret-db-detail" not in response.text


def test_access_log_omits_token_query_and_body(caplog, capfd):
    caplog.set_level(logging.INFO, logger="qingyu.access")
    client = TestClient(app)
    client.get(f"/health?token={QUERY}", headers={"Authorization": f"Bearer {TOKEN}", "X-Request-ID": "access-1"})
    client.post(
        "/api/v1/protected-ping",
        content=BODY.encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "X-Request-ID": "access-2"},
    )
    captured = capfd.readouterr()
    access_records = [record for record in caplog.records if record.name == "qingyu.access"]
    access_lines = [
        line
        for line in (captured.out + "\n" + captured.err).splitlines()
        if "qingyu.access" in line
    ]
    rendered = "\n".join(record.getMessage() for record in access_records) + "\n" + "\n".join(access_lines)
    assert any(getattr(record, "request_id", None) == "access-1" for record in access_records)
    assert "path=/health" in rendered
    assert "status=200" in rendered
    assert "client=testclient" in rendered
    assert TOKEN not in rendered
    assert BODY not in rendered
    assert QUERY not in rendered
    assert "Authorization" not in rendered
    assert "Bearer" not in rendered
    assert "?" not in "\n".join(record.getMessage() for record in access_records)


def test_json_access_line_is_json_without_the_token(caplog):
    caplog.set_level(logging.INFO, logger="qingyu.access")
    configure_logging("INFO", "json")
    try:
        handler = next(item for item in logging.getLogger().handlers if getattr(item, "_qingyu_handler", False))
        assert isinstance(handler.formatter, JsonFormatter)
        response = TestClient(app).get("/health", headers={"Authorization": f"Bearer {TOKEN}", "X-Request-ID": "json-1"})
        records = [record for record in caplog.records if record.name == "qingyu.access"]
        assert records
        line = JsonFormatter().format(records[-1])
    finally:
        configure_logging("INFO", "text")
    assert response.headers["x-request-id"] == "json-1"
    payload = json.loads(line)
    assert payload["request_id"] == "json-1"
    assert payload["logger"] == "qingyu.access"
    assert "path=/health" in payload["message"]
    assert TOKEN not in line
    assert "Bearer" not in line
    assert QUERY not in line


def test_metrics_are_off_until_enabled_and_can_require_a_token(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "metrics_enabled", False)
    monkeypatch.setattr(settings, "metrics_token", "metrics-secret-value")
    client = TestClient(app)
    hidden = client.get("/metrics", headers={"Authorization": "Bearer metrics-secret-value"})
    assert hidden.status_code == 404
    assert "metrics-secret-value" not in hidden.text
    assert "/metrics" not in client.get("/openapi.json").text

    monkeypatch.setattr(settings, "metrics_enabled", True)
    app.state.metrics.reset()
    denied = client.get("/metrics")
    wrong = client.get("/metrics", headers={"Authorization": "Bearer wrong-token"})
    api_token = client.get("/metrics", headers=API_HEADERS)
    assert denied.status_code == 401
    assert wrong.status_code == 401
    assert api_token.status_code == 401
    assert "metrics-secret-value" not in denied.text + wrong.text + api_token.text

    health = client.get("/health", headers={"X-Request-ID": "metrics-health"})
    job_id = uuid.uuid4()
    missing = client.get(f"/api/v1/import-jobs/{job_id}", headers=API_HEADERS)
    unknown = client.get("/not-a-real-route")
    allowed = client.get("/metrics", headers={"Authorization": "Bearer metrics-secret-value"})
    assert health.status_code == 200
    assert missing.status_code == 404
    assert unknown.status_code == 404
    assert allowed.status_code == 200
    assert allowed.headers["content-type"].startswith("text/plain")
    body = allowed.text
    assert "metrics-secret-value" not in body
    assert 'qingyu_http_requests_total{route="/health",status="200"}' in body
    assert 'qingyu_http_request_duration_seconds_count{route="/health",status="200"}' in body
    assert 'qingyu_http_request_duration_seconds_bucket{route="/health",status="200",le="+Inf"}' in body
    assert 'route="/api/v1/import-jobs/{job_id}"' in body
    assert str(job_id) not in body
    assert 'route="unmatched",status="404"' in body
    assert 'route="/metrics",status="401"' in body

    monkeypatch.setattr(settings, "metrics_token", "")
    app.state.metrics.reset()
    client.get("/health")
    opened = client.get("/metrics")
    assert opened.status_code == 200
    assert 'route="/health"' in opened.text


def test_histogram_buckets_are_cumulative_and_thread_safe():
    registry = MetricsRegistry()
    registry.observe("/health", 200, 0.01)
    body = registry.render()
    assert 'qingyu_http_requests_total{route="/health",status="200"} 1' in body
    assert 'le="0.005"' in body and 'route="/health",status="200",le="0.005"} 0' in body
    assert 'le="0.01"} 1' in body
    assert 'le="+Inf"} 1' in body
    assert 'qingyu_http_request_duration_seconds_count{route="/health",status="200"} 1' in body

    def worker():
        registry.observe("/health", 200, 0.001)

    threads = [threading.Thread(target=worker) for _ in range(30)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert 'qingyu_http_requests_total{route="/health",status="200"} 31' in registry.render()
