import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient, models

from app.main import app, get_search_backend
from app.retrieval import CatalogueSearch, SearchBusy, SearchRequest, SearchUnavailable, VECTOR_NAME, DIMENSION

TEST_KEY = "test-only-not-a-real-secret-" + "a" * 32


class TestEncoder:
    __test__ = False
    profile_id = "offline-test-profile"

    def vector(self, text):
        return [1.0] + [0.0] * (DIMENSION - 1)


@pytest.fixture
def backend():
    qdrant = QdrantClient(":memory:")
    qdrant.create_collection("cag_catalogue_v1", vectors_config={VECTOR_NAME:
        models.VectorParams(size=DIMENSION, distance=models.Distance.COSINE)})
    for i in range(1, 8):
        qdrant.upsert("cag_catalogue_v1", points=[models.PointStruct(id=i,
            vector={VECTOR_NAME: [1.0, i / 10] + [0.0] * (DIMENSION - 2)}, payload={
                "document_id": f"document-{i}", "text": f"Offline test passage {i}",
                "title": f"Test audit report {i}", "page": i, "page_end": i + 1,
                "page_labels": [str(i), str(i + 1)],
                "source_url": f"https://cag.gov.in/uploads/download_audit_report/report-{i}.pdf",
                "report_url": f"https://cag.gov.in/en/audit-report/details/{i}",
                "jurisdiction": "manipur" if i % 2 else "nagaland",
                "categories": ["state-finances"], "sectors": ["finance"],
                "published_year": 2026, "language": "en", "coverage": "partial",
                "embedding_profile": TestEncoder.profile_id, "ready": i != 7,
            })])
    backend = CatalogueSearch(client=qdrant, encoder_factory=TestEncoder)
    yield backend
    backend.close()


@pytest.fixture
def client(monkeypatch, backend):
    monkeypatch.setenv("RAG_API_KEY", TEST_KEY)
    app.dependency_overrides[get_search_backend] = lambda: backend
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def search(client, body):
    return client.post("/search", json=body, headers={"x-api-key": TEST_KEY})


def test_search_contract(client):
    response = search(client, {"query": "How much was allocated to healthcare?", "top_k": 5})
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert {"text", "title", "page", "source_url", "score", "citation_url", "coverage"} <= set(result)
    assert result["title"] == "Test audit report 1"
    assert result["text"] == "Offline test passage 1"
    assert result["page"] == 1
    assert result["page_end"] == 2
    assert result["citation_url"].endswith("report-1.pdf#page=1")
    assert response.json()["collection"] == "cag_catalogue_v1"
    assert any("partially extracted" in warning for warning in response.json()["warnings"])
    assert 0 <= result["score"] <= 1


def test_top_k_and_order(client):
    results = search(client, {"query": "allocation healthcare", "top_k": 2}).json()["results"]
    assert len(results) == 2
    assert results[0]["score"] >= results[1]["score"]


def test_no_matching_filter_or_threshold(client):
    assert search(client, {"query": "xyzzy", "jurisdiction": "not-indexed"}).json()["results"] == []
    assert search(client, {"query": "xyzzy", "score_threshold": 1.0}).json()["results"] == []


def test_default_top_k_and_whitespace(client):
    assert len(search(client, {"query": "  allocation  "}).json()["results"]) == 5


@pytest.mark.parametrize("body", [
    {}, {"query": ""}, {"query": " \t "}, {"query": "x" * 2001},
    {"query": "health", "top_k": 0}, {"query": "health", "top_k": 21},
    {"query": "health", "top_k": True}, {"query": "health", "top_k": "5"},
    {"query": "health", "top_k": 1.5}, {"query": "health", "unexpected": 1},
    {"query": "health", "jurisdiction": " "}, {"query": "health", "year": "2026"},
    {"query": "health", "year": True}, {"query": "health", "year": 100},
    {"query": "health", "score_threshold": 1.1}, {"query": "health", "category": "x" * 101},
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
    assert schema["paths"]["/ready"]["get"]["security"] == [{"BudgetApiKey": []}]
    assert "synthetic" not in schema["info"]["description"].lower()


def test_combined_filters_and_unpublished_exclusion(client):
    body = {"query": "fiscal deficit", "jurisdiction": " MANIPUR ",
            "category": "State Finances", "sector": "Finance", "language": "EN", "year": 2026, "top_k": 20}
    results = search(client, body).json()["results"]
    assert [hit["id"] for hit in results] == ["1", "3", "5"]
    assert all(hit["jurisdiction"] == "manipur" for hit in results)
    assert search(client, {**body, "year": 2025}).json()["results"] == []


def test_ready_counts_only_published_chunks(client):
    response = client.get("/ready", headers={"x-api-key": TEST_KEY})
    assert response.json() == {"status": "ready", "collection": "cag_catalogue_v1", "searchable_chunks": 6}
    assert client.get("/ready").status_code == 401


def test_auth_does_not_initialize_model(client, backend):
    backend.encoder_factory = Mock(side_effect=AssertionError("must not load"))
    assert client.post("/search", json={"query": "health"}).status_code == 401
    assert client.get("/health").status_code == 200
    backend.encoder_factory.assert_not_called()


def test_profile_mismatch_fails_closed(client, backend):
    backend.client.set_payload("cag_catalogue_v1", payload={"embedding_profile": "wrong-model"}, points=[1])
    assert search(client, {"query": "health"}).status_code == 503
    assert client.get("/ready", headers={"x-api-key": TEST_KEY}).status_code == 503


def test_schema_mismatch_fails_closed(client, backend):
    backend.client.delete_collection("cag_catalogue_v1")
    backend.client.create_collection("cag_catalogue_v1", vectors_config=models.VectorParams(size=512, distance=models.Distance.COSINE))
    assert search(client, {"query": "health"}).status_code == 503


def test_backend_failure_does_not_leak_details(client, backend):
    backend.encoder_factory = Mock(side_effect=RuntimeError("secret-internal-path-or-key"))
    response = search(client, {"query": "health"})
    assert response.status_code == 503
    assert "secret-internal" not in response.text
    assert client.get("/health").status_code == 200


def test_busy_worker_returns_retry_after(client, backend):
    with backend.lock:
        response = search(client, {"query": "health"})
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "2"
    assert search(client, {"query": "health"}).status_code == 200


def test_model_loaded_once_per_backend(client, backend):
    factory = Mock(return_value=TestEncoder())
    backend.encoder_factory = factory
    assert search(client, {"query": "first"}).status_code == 200
    assert search(client, {"query": "second"}).status_code == 200
    factory.assert_called_once()


def test_bad_citation_fails_closed(client, backend):
    backend.client.set_payload("cag_catalogue_v1", payload={"source_url": "https://example.org/not-cag"}, points=[1])
    assert search(client, {"query": "health"}).status_code == 503


def test_empty_collection_readiness_unavailable(client, backend):
    backend.client.delete("cag_catalogue_v1", points_selector=list(range(1, 8)))
    assert client.get("/ready", headers={"x-api-key": TEST_KEY}).status_code == 503


def test_negative_cosine_scores_are_supported(client, backend):
    backend.client.upsert("cag_catalogue_v1", points=[models.PointStruct(id=8,
        vector={VECTOR_NAME: [-1.0] + [0.0] * (DIMENSION - 1)},
        payload={**backend.client.retrieve("cag_catalogue_v1", ids=[1])[0].payload, "jurisdiction": "negative"})])
    response = search(client, {"query": "health", "jurisdiction": "negative"})
    assert response.status_code == 200
    assert response.json()["results"][0]["score"] == -1.0


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