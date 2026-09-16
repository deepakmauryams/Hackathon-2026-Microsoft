"""Deterministic mock retrieval. Replace search_documents with a DB/vector adapter later."""

import re

from pydantic import BaseModel, ConfigDict, Field


class SearchRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20, strict=True)


class SearchResult(BaseModel):
    text: str
    title: str
    page: int = Field(ge=1)
    source_url: str
    score: float = Field(ge=0, le=1)


class SearchResponse(BaseModel):
    results: list[SearchResult]


# Fictional fixtures, not extracted budget facts or real document citations.
DOCUMENTS = (
    ("Healthcare", "healthcare health hospital medical allocation",
     "The fictional healthcare allocation is 100 demo units (Budget Estimates).", 42),
    ("Education", "education school university learning allocation",
     "The fictional education allocation is 80 demo units (Budget Estimates).", 18),
    ("Agriculture", "agriculture farming farmers crops allocation",
     "The fictional agriculture allocation is 60 demo units (Revised Estimates).", 23),
    ("Infrastructure", "infrastructure roads transport railway allocation",
     "The fictional infrastructure expenditure is 120 demo units (Actuals).", 31),
    ("Budget Terminology", "budget estimates revised actuals expenditure allocation",
     "This fixture distinguishes proposed Budget Estimates, updated Revised Estimates, "
     "and recorded Actuals. It contains no real budget figures.", 2),
)


def search_documents(query: str, top_k: int) -> list[SearchResult]:
    """Rank keyword overlap for testing only; scores are not vector similarities."""
    tokens = set(re.findall(r"\w+", query.casefold()))
    ranked = []
    for title, keywords, passage, page in DOCUMENTS:
        matches = tokens.intersection(keywords.split())
        if matches:
            ranked.append(SearchResult(
                text=f"SYNTHETIC TEST DATA — NOT REAL BUDGET FACTS. {passage}",
                title=f"DEMO ONLY — {title}",
                page=page,
                source_url=f"https://example.com/mock-budget/{title.lower().replace(' ', '-')}",
                score=round(len(matches) / len(tokens), 4),
            ))
    return sorted(ranked, key=lambda result: result.score, reverse=True)[:top_k]