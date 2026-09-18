"""Agent contract regression tests; schema generation never needs a model or Qdrant."""

import copy
import json
import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.openapi_tools import SEARCH_EXAMPLES, search_tool_schema
from app.retrieval import SearchRequest, SearchResponse


@pytest.fixture
def schema():
    result = copy.deepcopy(app.openapi())
    result["servers"] = [{"url": "https://api.example.com"}]
    return result


def test_all_search_fields_have_useful_descriptions(schema):
    for name in ("SearchRequest", "SearchResult", "SearchResponse"):
        for field, definition in schema["components"]["schemas"][name]["properties"].items():
            assert len(definition.get("description", "")) >= 20, (name, field)
    request = schema["components"]["schemas"]["SearchRequest"]
    assert request["required"] == ["query"]
    assert request["additionalProperties"] is False
    assert "PUBLICATION" in schema["paths"]["/search"]["post"]["description"]
    assert "not fiscal/audit" in schema["paths"]["/search"]["post"]["description"]


def test_request_examples_validate_and_do_not_invent_audit_year_filter(schema):
    examples = schema["paths"]["/search"]["post"]["requestBody"]["content"]["application/json"]["examples"]
    assert examples == SEARCH_EXAMPLES
    for example in examples.values():
        SearchRequest.model_validate(example["value"])
        assert "x-api-key" not in example["value"]
    assert "year" not in examples["audit_period"]["value"]
    assert examples["publication_year"]["value"]["year"] == 2026


def test_search_only_spec_has_resolved_refs_and_one_auth_scheme(schema):
    original = copy.deepcopy(schema)
    tool = search_tool_schema(schema)
    assert schema == original  # no mutation of the full docs
    assert tool["openapi"].startswith("3.1.")
    assert tool["servers"] == [{"url": "https://api.example.com"}]
    assert set(tool["paths"]) == {"/search"}
    assert tool["security"] == [{"BudgetApiKey": []}]
    assert list(tool["components"]["securitySchemes"]) == ["BudgetApiKey"]
    op = tool["paths"]["/search"]["post"]
    assert re.fullmatch("[A-Za-z_-]+", op["operationId"])
    assert op["operationId"] == "search_budget_documents"
    assert op["security"] == tool["security"]
    assert "HealthResponse" not in tool["components"]["schemas"]
    assert set(op["requestBody"]["content"]) == {"application/json"}
    for ref in re.findall(r'"\$ref": "([^"]+)"', json.dumps(tool)):
        assert ref.startswith("#/components/schemas/")
        assert ref.rsplit("/", 1)[1] in tool["components"]["schemas"]
    for code in ("200", "401", "422", "429", "503"):
        assert "schema" in op["responses"][code]["content"]["application/json"]
    SearchResponse.model_validate(op["responses"]["200"]["content"]["application/json"]["examples"]["no_matches"]["value"])


def test_missing_public_origin_refuses_import_spec(schema):
    schema.pop("servers")
    with pytest.raises(ValueError, match="PUBLIC_BASE_URL"):
        search_tool_schema(schema)


def test_public_schema_endpoint_no_secrets_or_backend(monkeypatch, schema):
    monkeypatch.setenv("RAG_API_KEY", "secret-must-not-appear-in-openapi-123456789")
    monkeypatch.setattr(app, "openapi", lambda: schema)
    with TestClient(app) as client:
        response = client.get("/openapi-foundry.json")
        assert response.status_code == 200
        assert set(response.json()["paths"]) == {"/search"}
        assert "secret-must-not-appear" not in response.text
        assert client.app.state.catalogue_search.encoder is None
        schema.pop("servers")
        assert client.get("/openapi-foundry.json").status_code == 503


def test_generated_openapi_is_valid(schema):
    from openapi_spec_validator import validate
    validate(schema)
    validate(search_tool_schema(schema))