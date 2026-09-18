"""Read-only model/profile/index/search gate; also runs in the API service sandbox."""

import json
import os
from pathlib import Path
import sys


def verify():
    release = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(release))
    expected = json.loads((release / "snapshot/profile.json").read_text())
    if list(sys.version_info[:2]) != expected["python"]:
        raise RuntimeError("Release and worker Python major/minor versions differ")
    if os.environ.get("CAG_MODEL_PATH") != str(release / "snapshot/model"):
        raise RuntimeError("Explicit offline snapshot path is required")
    from ingestion.catalogue_encoder import CatalogueEncoder
    from app.retrieval import CatalogueSearch, SearchRequest
    from app.main import app  # Validate imports and PUBLIC_BASE_URL without inspecting API keys.

    encoder = CatalogueEncoder()
    if Path(encoder.model.model._model_dir).resolve() != (release / "snapshot/model").resolve():
        raise RuntimeError("Encoder ignored CAG_MODEL_PATH; offline-path support is required")
    if encoder.profile_id != expected["profile_id"] or encoder.profile != expected["profile"]:
        raise RuntimeError("Release embedding profile differs from the installed worker")
    backend = CatalogueSearch(encoder_factory=lambda: encoder)
    try:
        readiness = backend.ready()
        result = backend.search(SearchRequest(query="public expenditure audit", top_k=1))
        if not result.results:
            raise RuntimeError("Read-only smoke search returned no published passages")
        print(f"Verified offline profile and read-only search; searchable chunks: {readiness['searchable_chunks']}")
    finally:
        backend.close()


if __name__ == "__main__":
    verify()