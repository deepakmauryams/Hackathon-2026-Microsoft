"""HTTP endpoints for the hackathon API."""

import os
import secrets
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from app.retrieval import DOCUMENTS, SearchRequest, SearchResponse, search_documents

public_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
if public_url:
    parsed = urlsplit(public_url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path):
        raise ValueError("PUBLIC_BASE_URL must be an HTTPS origin without credentials or path")

app = FastAPI(
    title="Hackathon 2026 API",
    description="Mock budget retrieval API. All passages, citations and scores are synthetic test data.",
    version="1.1.0",
    servers=[{"url": public_url}] if public_url else [],
)

api_key_header = APIKeyHeader(name="x-api-key", scheme_name="BudgetApiKey", auto_error=False)


def require_api_key(key: Annotated[str | None, Security(api_key_header)]) -> None:
    expected = os.getenv("RAG_API_KEY", "")
    if len(expected) < 32:
        raise HTTPException(status_code=503, detail="Search authentication is not configured")
    if key is None or not secrets.compare_digest(key.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


class HealthResponse(BaseModel):
    status: Literal["healthy"] = "healthy"
    documents: int
    chunks: int


@app.get("/", tags=["General"])
def root() -> dict[str, str]:
    """Return API information and a link to the interactive documentation."""
    return {"message": "Welcome to Hackathon 2026 API", "docs": "/docs"}


@app.get("/health", tags=["Health"], response_model=HealthResponse, operation_id="get_health")
def health() -> HealthResponse:
    """Liveness plus fixture counts; these are not database/index readiness checks."""
    return HealthResponse(documents=len(DOCUMENTS), chunks=len(DOCUMENTS))


@app.post(
    "/search",
    tags=["Retrieval"],
    operation_id="search_budget_documents",
    response_model=SearchResponse,
    dependencies=[Security(require_api_key)],
    responses={401: {"description": "Invalid or missing API key"},
               413: {"description": "Request body exceeds the proxy limit"},
               429: {"description": "Proxy rate limit exceeded"},
               503: {"description": "Search authentication is not configured"}},
)
def search(request: SearchRequest) -> SearchResponse:
    """Search SYNTHETIC budget fixtures, not real budget evidence. No match returns an empty list."""
    return SearchResponse(results=search_documents(request.query, request.top_k))


@app.get("/api/hello", tags=["Demo"])
def hello(
    name: Annotated[str, Query(min_length=1, max_length=100)] = "World",
) -> dict[str, str]:
    """Return a greeting with optional query-string input."""
    return {"message": f"Hello, {name}!"}