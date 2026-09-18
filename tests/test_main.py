import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("RAG_API_KEY", "test-only-key-" + "a" * 32)
    with TestClient(app) as test_client:
        yield test_client


def test_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {
        "message": "Welcome to Hackathon 2026 API",
        "docs": "/docs",
    }


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_default_greeting(client):
    response = client.get("/api/hello")
    assert response.status_code == 200
    assert response.json() == {"message": "Hello, World!"}


@pytest.mark.parametrize("name", ["Deepak", "Ada Lovelace", "世界", "a" * 100])
def test_named_greeting(client, name):
    response = client.get("/api/hello", params={"name": name})
    assert response.status_code == 200
    assert response.json() == {"message": f"Hello, {name}!"}


@pytest.mark.parametrize("name", ["", "a" * 101])
def test_invalid_name(client, name):
    response = client.get("/api/hello", params={"name": name})
    assert response.status_code == 422


def test_openapi_schema(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert {"/", "/health", "/api/hello"} <= response.json()["paths"].keys()


def test_interactive_docs(client):
    response = client.get("/docs")
    assert response.status_code == 200
    assert "swagger-ui" in response.text


def test_unknown_route(client):
    assert client.get("/not-found").status_code == 404