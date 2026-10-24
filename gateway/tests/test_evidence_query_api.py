import base64
import json
import uuid
from datetime import UTC, datetime

import pytest
from app.config import Settings
from app.db import get_db
from app.evidence.cursor import encode_cursor
from app.evidence.query import (
    AttachmentObservationQueryPage,
    EvidenceIntegrityError,
    EvidenceQueryService,
    QueryPage,
)
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

READ_HEADERS = {"Authorization": "Bearer qingyu-test-evidence-read-token-only"}
ADMIN_HEADERS = {"Authorization": "Bearer qingyu-test-token-only"}


def _summary(**overrides):
    now = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    values = {
        "conversation_observation_id": uuid.uuid4(),
        "import_batch_commit_id": uuid.uuid4(),
        "conversation_id": uuid.uuid4(),
        "source_account_id": uuid.uuid4(),
        "archive_object_id": uuid.uuid4(),
        "source_platform": "chatgpt",
        "parser_name": "chatgpt-export",
        "parser_contract_version": "1",
        "committed_at": now,
        "observed_at": now,
        "observed_title": "historical title",
        "normalized_created_at": now,
        "normalized_updated_at": None,
        "create_time_parse_status": "parsed",
        "update_time_parse_status": "absent",
        "current_conversation_node_id": uuid.uuid4(),
        "current_node_status": "resolved",
        "source_observation_fingerprint": "a" * 64,
        "canonical_fragment_fingerprint": "b" * 64,
        "fragment_fingerprint_version": "1",
    }
    values.update(overrides)
    return values


def test_evidence_routes_accept_read_and_admin_tokens_but_read_token_cannot_post(monkeypatch):
    monkeypatch.setattr(
        EvidenceQueryService,
        "list_conversation_observations",
        lambda *_args, **_kwargs: QueryPage(items=[], has_more=False),
    )
    app.dependency_overrides[get_db] = lambda: object()
    try:
        client = TestClient(app)
        assert client.get(
            "/api/v1/evidence/conversation-observations", headers=READ_HEADERS
        ).status_code == 200
        assert client.get(
            "/api/v1/evidence/conversation-observations", headers=ADMIN_HEADERS
        ).status_code == 200
        unauthorized = client.get("/api/v1/evidence/conversation-observations")
        assert unauthorized.status_code == 401
        assert unauthorized.headers["cache-control"] == "private, no-store"
        wrong = client.get(
            "/api/v1/evidence/conversation-observations",
            headers={"Authorization": "Bearer wrong"},
        )
        assert wrong.json() == {"detail": {"code": "authentication_required"}}
        assert wrong.headers["cache-control"] == "private, no-store"
        assert client.post("/api/v1/import/chatgpt", headers=READ_HEADERS).status_code == 401
        assert client.post("/api/v1/archive/objects", headers=READ_HEADERS).status_code == 401
        assert client.get("/api/v1/protected-ping", headers=READ_HEADERS).status_code == 401
        assert client.get(
            f"/api/v1/import-jobs/{uuid.uuid4()}", headers=READ_HEADERS
        ).status_code == 401
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_production_requires_distinct_non_example_evidence_read_token():
    with pytest.raises(ValueError, match="EVIDENCE_READ_API_TOKEN"):
        Settings(
            app_env="production",
            api_token="admin-production-secret",
            evidence_read_api_token="",
            evidence_cursor_signing_key="server-only-cursor-secret",
            _env_file=None,
        )
    with pytest.raises(ValueError, match="must differ"):
        Settings(
            app_env="production",
            api_token="same-production-secret",
            evidence_read_api_token="same-production-secret",
            evidence_cursor_signing_key="server-only-cursor-secret",
            _env_file=None,
        )
    with pytest.raises(ValueError, match="EVIDENCE_CURSOR_SIGNING_KEY"):
        Settings(
            app_env="production",
            api_token="admin-production-secret",
            evidence_read_api_token="read-production-secret",
            evidence_cursor_signing_key="admin-production-secret",
            _env_file=None,
        )
    with pytest.raises(ValueError, match="EVIDENCE_CURSOR_SIGNING_KEY"):
        Settings(
            app_env="production",
            api_token="admin-production-secret",
            evidence_read_api_token="read-production-secret",
            evidence_cursor_signing_key="read-production-secret",
            _env_file=None,
        )
    settings = Settings(
        app_env="production",
        api_token="admin-production-secret",
        evidence_read_api_token="read-production-secret",
        evidence_cursor_signing_key="server-only-cursor-secret",
        _env_file=None,
    )
    assert settings.evidence_read_api_token == "read-production-secret"


def test_observation_cursor_is_bounded_high_water_and_tamper_evident(monkeypatch):
    first = _summary()
    upper = (first["committed_at"], first["conversation_observation_id"])
    continuation_calls = []

    def fake_list(*_args, **kwargs):
        if kwargs["after"] is None:
            return QueryPage(items=[first], has_more=True, upper_bound=upper)
        continuation_calls.append(kwargs)
        return QueryPage(items=[], has_more=False, upper_bound=kwargs["upper_bound"])

    monkeypatch.setattr(EvidenceQueryService, "list_conversation_observations", fake_list)
    app.dependency_overrides[get_db] = lambda: object()
    try:
        client = TestClient(app)
        response = client.get(
            "/api/v1/evidence/conversation-observations?limit=1", headers=READ_HEADERS
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        assert response.json()["consistency"] == "bounded_high_water_enumeration_v1"
        cursor = response.json()["next_cursor"]
        assert cursor
        encoded_payload = cursor.split(".", 1)[0]
        raw_payload = base64.urlsafe_b64decode(
            encoded_payload + "=" * (-len(encoded_payload) % 4)
        )
        payload = json.loads(raw_payload)
        for bearer_secret in (
            "qingyu-test-evidence-read-token-only",
            "qingyu-test-token-only",
        ):
            forged = encode_cursor(
                {key: value for key, value in payload.items() if key != "v"},
                bearer_secret,
            )
            assert client.get(
                "/api/v1/evidence/conversation-observations",
                headers=READ_HEADERS,
                params={"cursor": forged},
            ).status_code == 400
        assert client.get(
            "/api/v1/evidence/conversation-observations",
            headers=READ_HEADERS,
            params={"cursor": cursor},
        ).status_code == 200
        assert continuation_calls[-1]["upper_bound"] == upper
        assert continuation_calls[-1]["after"] == (
            first["committed_at"],
            first["conversation_observation_id"],
        )
        assert continuation_calls[-1]["source_account_id"] is None
        assert continuation_calls[-1]["conversation_id"] is None
        assert client.get(
            "/api/v1/evidence/conversation-observations",
            headers=READ_HEADERS,
            params={"cursor": cursor, "conversation_id": str(uuid.uuid4())},
        ).json() == {"detail": {"code": "evidence_cursor_invalid"}}
        payload_part, signature_part = cursor.split(".", 1)
        # A SHA-256 signature is 32 bytes, so the final base64 character has
        # unused low bits. Replacing that character with A or B can decode to
        # the same signature. Flip the first signature character instead.
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        replacement = alphabet[(alphabet.index(signature_part[0]) + 1) % len(alphabet)]
        tampered = f"{payload_part}.{replacement}{signature_part[1:]}"
        assert client.get(
            "/api/v1/evidence/conversation-observations",
            headers=READ_HEADERS,
            params={"cursor": tampered},
        ).status_code == 400
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_attachment_attempts_are_observation_scoped_and_keyset_paginated(monkeypatch):
    observation_id = uuid.uuid4()
    attachment_id = uuid.uuid4()
    other_observation_id = uuid.uuid4()
    now = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    attempts = [
        {
            "attachment_observation_id": uuid.uuid4(),
            "attempt_number": number,
            "availability": "available",
            "archive_object_id": uuid.uuid4(),
            "observed_filename": "photo.png",
            "observed_media_type": "image/png",
            "observed_size_bytes": 12,
            "resolution_status": "available",
            "canonical_fragment_fingerprint": str(number) * 64,
            "fragment_fingerprint_version": "1",
            "observed_at": now,
        }
        for number in (1, 2)
    ]
    upper = (
        attempts[-1]["attempt_number"],
        attempts[-1]["observed_at"],
        attempts[-1]["attachment_observation_id"],
    )

    def fake_list(*_args, **kwargs):
        if kwargs["conversation_observation_id"] != observation_id:
            return None
        items = attempts[:1] if kwargs["after"] is None else attempts[1:]
        return AttachmentObservationQueryPage(
            attachment={
                "attachment_id": attachment_id,
                "filename": "photo.png",
                "media_type": "image/png",
                "size_bytes": 12,
            },
            items=items,
            has_more=kwargs["after"] is None,
            upper_bound=upper,
        )

    monkeypatch.setattr(EvidenceQueryService, "list_attachment_observations", fake_list)
    app.dependency_overrides[get_db] = lambda: object()
    try:
        client = TestClient(app)
        path = (
            f"/api/v1/evidence/conversation-observations/{observation_id}"
            f"/attachments/{attachment_id}/observations"
        )
        first = client.get(path, headers=READ_HEADERS, params={"limit": 1})
        assert first.status_code == 200
        assert first.headers["cache-control"] == "private, no-store"
        assert first.json()["consistency"] == "bounded_high_water_enumeration_v1"
        assert len(first.json()["items"]) == 1
        second = client.get(
            path,
            headers=READ_HEADERS,
            params={"limit": 1, "cursor": first.json()["next_cursor"]},
        )
        assert second.status_code == 200
        assert second.json()["items"][0]["attempt_number"] == 2
        cross_scope = client.get(
            path.replace(str(observation_id), str(other_observation_id)),
            headers=READ_HEADERS,
        )
        assert cross_scope.status_code == 404
        assert cross_scope.json() == {"detail": {"code": "evidence_attachment_not_found"}}
        assert client.get(path, headers=READ_HEADERS, params={"limit": 0}).status_code == 422
        assert client.get(path, headers=READ_HEADERS, params={"limit": 101}).status_code == 422
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.parametrize(
    ("setup", "path", "expected_status"),
    [
        ("cursor", "/api/v1/evidence/conversation-observations?cursor=bad", 400),
        ("auth", "/api/v1/evidence/conversation-observations", 401),
        ("not_found", None, 404),
        ("validation", "/api/v1/evidence/conversation-observations?limit=0", 422),
    ],
)
def test_evidence_error_matrix_is_never_cached(monkeypatch, setup, path, expected_status):
    observation_id = uuid.uuid4()
    if setup == "not_found":
        monkeypatch.setattr(
            EvidenceQueryService, "list_observation_nodes", lambda *_args, **_kwargs: None
        )
        path = f"/api/v1/evidence/conversation-observations/{observation_id}/nodes"
    app.dependency_overrides[get_db] = lambda: object()
    try:
        client = TestClient(app, raise_server_exceptions=False)
        headers = {} if setup == "auth" else READ_HEADERS
        response = client.get(path, headers=headers)
        assert response.status_code == expected_status
        assert response.headers["cache-control"] == "private, no-store"
        assert "reference:/server/secret" not in response.text
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.parametrize(
    ("failure_kind", "expected_code"),
    [
        ("database", "database_operation_failed"),
        ("integrity", "evidence_integrity_violation"),
        ("unhandled", "evidence_operation_failed"),
    ],
)
def test_evidence_500_contract_and_logs_are_safe(
    monkeypatch, caplog, failure_kind, expected_code
):
    secret = "token=private-token path=D:/private member:/secret parameter=hidden"

    if failure_kind == "database":
        def fail(*_args, **_kwargs):
            raise SQLAlchemyError(secret)

        monkeypatch.setattr(EvidenceQueryService, "list_conversation_observations", fail)
        path = "/api/v1/evidence/conversation-observations"
    elif failure_kind == "integrity":
        def fail(*_args, **_kwargs):
            raise EvidenceIntegrityError(secret)

        monkeypatch.setattr(EvidenceQueryService, "list_observation_nodes", fail)
        path = f"/api/v1/evidence/conversation-observations/{uuid.uuid4()}/nodes"
    else:
        def fail(*_args, **_kwargs):
            raise RuntimeError(secret)

        monkeypatch.setattr(EvidenceQueryService, "list_conversation_observations", fail)
        path = "/api/v1/evidence/conversation-observations"

    app.dependency_overrides[get_db] = lambda: object()
    try:
        caplog.clear()
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(path, headers=READ_HEADERS)
        assert response.status_code == 500
        assert response.headers["cache-control"] == "private, no-store"
        detail = response.json()["detail"]
        assert detail["code"] == expected_code
        assert isinstance(detail["request_id"], str)
        assert len(detail["request_id"]) == 32
        for forbidden in ("private-token", "D:/private", "member:/secret", "hidden"):
            assert forbidden not in response.text
            assert forbidden not in caplog.text
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_node_not_found_and_limit_validation_are_stable(monkeypatch):
    monkeypatch.setattr(
        EvidenceQueryService,
        "list_observation_nodes",
        lambda *_args, **_kwargs: None,
    )
    app.dependency_overrides[get_db] = lambda: object()
    try:
        client = TestClient(app)
        observation_id = uuid.uuid4()
        response = client.get(
            f"/api/v1/evidence/conversation-observations/{observation_id}/nodes",
            headers=READ_HEADERS,
        )
        assert response.status_code == 404
        assert response.json() == {"detail": {"code": "evidence_observation_not_found"}}
        assert client.get(
            "/api/v1/evidence/conversation-observations?limit=101", headers=READ_HEADERS
        ).status_code == 422
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_openapi_evidence_dtos_are_explicit_privacy_allowlists():
    schema = TestClient(app).get("/openapi.json").json()["components"]["schemas"]
    evidence_models = {
        name: value
        for name, value in schema.items()
        if name
        in {
            "ObservationPage",
            "NodePage",
            "ConversationObservationSummary",
            "NodeEvidence",
            "MessageRevisionEvidence",
            "AttachmentEvidence",
            "AttachmentAttemptEvidence",
            "AttachmentObservationPage",
        }
    }
    serialized = str(evidence_models).lower()
    for forbidden in (
        "relative_path",
        "source_member",
        "raw_json_pointer",
        "source_conversation_json",
        "source_node_json",
        "semantic_evidence_json",
        "message_observation_json",
        "metadata_json",
        "descriptor_json",
        "report_json",
        "lease_token",
        "database_url",
        "process_id",
        "stable_attachment_key",
        "original_reference",
    ):
        assert forbidden not in serialized
