from app import db
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError


def test_health() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_protected_endpoint_requires_token() -> None:
    response = TestClient(app).get("/api/v1/protected-ping")
    assert response.status_code == 401


class _FailingConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, *_args, **_kwargs):
        raise SQLAlchemyError("marker-should-not-leak")


class _FailingEngine:
    def connect(self):
        return _FailingConnection()


def test_health_ready_returns_503_when_database_raises(monkeypatch) -> None:
    monkeypatch.setattr(db, "engine", _FailingEngine())
    response = TestClient(app).get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert "marker-should-not-leak" not in response.text


def test_health_ready_returns_200_when_database_answers() -> None:
    response = TestClient(app).get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
