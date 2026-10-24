from app.main import app
from fastapi.testclient import TestClient


def test_health() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

def test_protected_endpoint_requires_token() -> None:
    response = TestClient(app).get("/api/v1/protected-ping")
    assert response.status_code == 401
