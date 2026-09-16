import os
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app.main import app

TEST_KEY = "test-only-not-a-real-secret-" + "a" * 32


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("RAG_API_KEY", TEST_KEY)
    with TestClient(app) as client:
        yield client


def search(client, body):
    return client.post("/search", json=body, headers={"x-api-key": TEST_KEY})


def test_search_contract(client):
    response = search(client, {"query": "How much was allocated to healthcare?", "top_k": 5})
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert set(result) == {"text", "title", "page", "source_url", "score"}
    assert "Healthcare" in result["title"]
    assert "SYNTHETIC TEST DATA" in result["text"]
    assert result["page"] == 42
    assert 0 <= result["score"] <= 1


def test_top_k_and_order(client):
    results = search(client, {"query": "allocation healthcare", "top_k": 2}).json()["results"]
    assert len(results) == 2
    assert results[0]["score"] >= results[1]["score"]


def test_unknown_query(client):
    assert search(client, {"query": "xyzzy"}).json() == {"results": []}


def test_default_top_k_and_whitespace(client):
    assert len(search(client, {"query": "  allocation  "}).json()["results"]) == 5


@pytest.mark.parametrize("body", [
    {}, {"query": ""}, {"query": " \t "}, {"query": "x" * 2001},
    {"query": "health", "top_k": 0}, {"query": "health", "top_k": 21},
    {"query": "health", "top_k": True}, {"query": "health", "top_k": "5"},
    {"query": "health", "top_k": 1.5}, {"query": "health", "unexpected": 1},
])
def test_validation(client, body):
    assert search(client, body).status_code == 422


@pytest.mark.parametrize("key", [None, "wrong"])
def test_auth_required(client, key):
    headers = {"x-api-key": key} if key else {}
    assert client.post("/search", json={"query": "health"}, headers=headers).status_code == 401


@pytest.mark.parametrize("value", [None, "", "short"])
def test_unconfigured_fails_closed(client, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("RAG_API_KEY")
    else:
        monkeypatch.setenv("RAG_API_KEY", value)
    assert search(client, {"query": "health"}).status_code == 503
    assert client.get("/health").status_code == 200


def test_openapi_tool_schema(client):
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/search"]["post"]
    assert operation["operationId"] == "search_budget_documents"
    assert operation["security"] == [{"BudgetApiKey": []}]
    assert schema["components"]["securitySchemes"]["BudgetApiKey"] == {
        "type": "apiKey", "in": "header", "name": "x-api-key",
    }
    assert TEST_KEY not in str(schema)
    assert "security" not in schema["paths"]["/health"]["get"]


def test_https_origin_in_schema():
    env = {**os.environ, "PUBLIC_BASE_URL": "https://budget.example.com/"}
    result = subprocess.run(
        [sys.executable, "-c", "from app.main import app; print(app.openapi()['servers'][0]['url'])"],
        cwd=Path(__file__).parents[1], env=env, capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "https://budget.example.com"


@pytest.mark.parametrize("origin", [
    "http://budget.example.com", "https://user:secret@example.com", "https://example.com/api",
])
def test_reject_invalid_public_origin(origin):
    result = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=Path(__file__).parents[1], env={**os.environ, "PUBLIC_BASE_URL": origin},
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "PUBLIC_BASE_URL must be an HTTPS origin" in result.stderr