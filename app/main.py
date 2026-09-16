"""HTTP endpoints for the hackathon API."""

from typing import Annotated

from fastapi import FastAPI, Query

app = FastAPI(
    title="Hackathon 2026 API",
    description="A starter Python API hosted on a virtual machine.",
    version="1.0.0",
)


@app.get("/", tags=["General"])
def root() -> dict[str, str]:
    """Return API information and a link to the interactive documentation."""
    return {"message": "Welcome to Hackathon 2026 API", "docs": "/docs"}


@app.get("/health", tags=["Health"])
def health() -> dict[str, str]:
    """Confirm that the application is running."""
    return {"status": "ok"}


@app.get("/api/hello", tags=["Demo"])
def hello(
    name: Annotated[str, Query(min_length=1, max_length=100)] = "World",
) -> dict[str, str]:
    """Return a greeting with optional query-string input."""
    return {"message": f"Hello, {name}!"}