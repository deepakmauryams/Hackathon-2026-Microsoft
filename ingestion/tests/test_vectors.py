from contextlib import closing
import uuid

import pytest
from qdrant_client import QdrantClient, models

from ingestion.io import read_jsonl, write_jsonl
from ingestion import vector_store as store


def artifacts(root):
    chunk = {"id": str(uuid.uuid4()), "text": "Example test text"}
    write_jsonl(root / "chunks.jsonl", [chunk])
    write_jsonl(root / "vectors.jsonl", [{"id": chunk["id"],
        "vector": [1.0] + [0.0] * (store.DIMENSION - 1),
        "payload": {**chunk, "embedding_profile": "test-profile"}}])
    write_jsonl(root / "embedding-profile.jsonl", [{"model": store.MODEL,
        "dimension": store.DIMENSION, "chunks": 1, "profile_id": "test-profile",
        "chunks_sha256": store.file_hash(root / "chunks.jsonl"),
        "vectors_sha256": store.file_hash(root / "vectors.jsonl")}])
    return chunk


def test_index_is_idempotent_and_rejects_stale_snapshot(tmp_path, monkeypatch):
    artifacts(tmp_path)
    monkeypatch.setattr(store, "QdrantClient", lambda **kwargs: QdrantClient(path=tmp_path / "qdrant"))
    assert store.index(tmp_path)["points"] == 1
    assert store.index(tmp_path)["points"] == 1
    artifacts(tmp_path)  # Different chunk UUID, same model.
    with pytest.raises(ValueError, match="stale data"):
        store.index(tmp_path)
    with closing(QdrantClient(path=tmp_path / "qdrant")) as client:
        assert client.count(store.COLLECTION, exact=True).count == 1


def test_changed_artifact_rejected_before_connection(tmp_path, monkeypatch):
    artifacts(tmp_path)
    write_jsonl(tmp_path / "chunks.jsonl", [{"text": "Changed content"}])
    monkeypatch.setattr(store, "QdrantClient", lambda **kwargs: pytest.fail("Should not connect"))
    with pytest.raises(ValueError, match="Artifacts changed"):
        store.index(tmp_path)


def test_refuse_different_collection_schema():
    with closing(QdrantClient(":memory:")) as client:
        client.create_collection(store.COLLECTION, vectors_config=models.VectorParams(
            size=384, distance=models.Distance.COSINE))
        with pytest.raises(ValueError, match="different embedding schema"):
            store.validate_collection(client, create=True)
        assert client.get_collection(store.COLLECTION).config.params.vectors.size == 384


@pytest.mark.parametrize("vector", [[1.0], [0.0] * store.DIMENSION,
                                  [float("nan")] * store.DIMENSION,
                                  [float("inf")] * store.DIMENSION])
def test_invalid_vectors(vector):
    with pytest.raises(ValueError, match="invalid dimension"):
        store.validate_vector(vector)


def test_no_silent_truncation():
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace

    encoder = store.LocalEncoder.__new__(store.LocalEncoder)
    encoder.counter = Tokenizer(WordLevel({"word": 0, "[UNK]": 1}, unk_token="[UNK]"))
    encoder.counter.pre_tokenizer = Whitespace()
    with pytest.raises(ValueError, match="no truncation allowed"):
        next(encoder.vectors(["word " * (store.CONTEXT + 1)]))


def test_compose_loopback_only():
    from pathlib import Path
    compose = (Path(store.__file__).parent / "compose.yaml").read_text()
    assert '"127.0.0.1:6333:6333"' in compose
    assert "6334:" not in compose
    assert '"0.0.0.0:' not in compose