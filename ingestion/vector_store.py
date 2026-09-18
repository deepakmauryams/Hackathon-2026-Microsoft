"""Local CPU embeddings and an isolated, loopback-only Qdrant snapshot."""

from contextlib import closing
import hashlib
from importlib.metadata import version
import json
import logging
import math
from pathlib import Path

from fastembed import TextEmbedding
from qdrant_client import QdrantClient, models
from tokenizers import Tokenizer

from ingestion.io import read_jsonl, write_jsonl

MODEL = "jinaai/jina-embeddings-v2-small-en"
DIMENSION = 512
CONTEXT = 8192
VECTOR_NAME = "jina_small_en_v2"
COLLECTION = "budget_document_chunks"
QDRANT_URL = "http://127.0.0.1:6333"
CACHE = Path(__file__).resolve().parent / ".cache" / "models"
LOG = logging.getLogger(__name__)


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class LocalEncoder:
    def __init__(self):
        self.model = TextEmbedding(model_name=MODEL, cache_dir=str(CACHE), threads=4)
        # The pinned FastEmbed version exposes its underlying ONNX tokenizer.
        # Clone without padding/truncation to count the actual model input tokens.
        self.counter = Tokenizer.from_str(self.model.model.tokenizer.to_str())
        self.counter.no_truncation()
        self.counter.no_padding()
        self.model.model.tokenizer.enable_truncation(max_length=CONTEXT)
        model_dir = Path(self.model.model._model_dir)
        files = [model_dir / name for name in
                 ("onnx/model.onnx", "tokenizer.json", "tokenizer_config.json", "config.json")]
        self.profile = {
            "model": MODEL, "dimension": DIMENSION, "context": CONTEXT,
            "fastembed_version": version("fastembed"),
            "onnxruntime_version": version("onnxruntime"),
            "algorithm": "fastembed-mean-pooling-v1",
            "model_files_sha256": {p.relative_to(model_dir).as_posix(): file_hash(p) for p in files},
        }
        self.profile_id = hashlib.sha256(json.dumps(self.profile, sort_keys=True).encode()).hexdigest()

    def vectors(self, texts: list[str]):
        for text in texts:
            count = len(self.counter.encode(text).ids)
            if count > CONTEXT:
                raise ValueError(f"Embedding input has {count} model tokens, exceeds {CONTEXT}; no truncation allowed")
        for vector in self.model.embed(texts, batch_size=8, parallel=None):
            values = vector.tolist()
            validate_vector(values)
            yield values


def validate_vector(vector):
    if (len(vector) != DIMENSION or not all(math.isfinite(value) for value in vector)
            or not any(value != 0 for value in vector)):
        raise ValueError("Embedding has invalid dimension, non-finite values or zero norm")


def embed(root: Path) -> dict:
    chunks = read_jsonl(root / "chunks.jsonl")
    if not chunks:
        raise ValueError("No chunks to embed; run crawl and parse first")
    encoder = LocalEncoder()

    def records():
        for start in range(0, len(chunks), 8):
            batch = chunks[start:start + 8]
            vectors = encoder.vectors([chunk["text"] for chunk in batch])
            for chunk, vector in zip(batch, vectors, strict=True):
                yield {"id": chunk["id"], "vector": vector,
                       "payload": {**chunk, "embedding_model": MODEL,
                                   "embedding_profile": encoder.profile_id}}
            LOG.info("Embedded %s/%s chunks", min(start + 8, len(chunks)), len(chunks))

    write_jsonl(root / "vectors.jsonl", records())
    profile = {**encoder.profile, "profile_id": encoder.profile_id, "chunks": len(chunks),
               "chunks_sha256": file_hash(root / "chunks.jsonl"),
               "vectors_sha256": file_hash(root / "vectors.jsonl")}
    write_jsonl(root / "embedding-profile.jsonl", [profile])
    return {"chunks": len(chunks), "model": MODEL, "dimension": DIMENSION}


def validate_collection(client: QdrantClient, create: bool = False):
    if not client.collection_exists(COLLECTION):
        if not create:
            raise ValueError("Collection not found; run index first")
        client.create_collection(COLLECTION, vectors_config={
            VECTOR_NAME: models.VectorParams(size=DIMENSION, distance=models.Distance.COSINE)})
    vectors = client.get_collection(COLLECTION).config.params.vectors
    if (not isinstance(vectors, dict) or set(vectors) != {VECTOR_NAME}
            or vectors[VECTOR_NAME].size != DIMENSION
            or vectors[VECTOR_NAME].distance != models.Distance.COSINE):
        raise ValueError("Existing collection uses a different embedding schema; refusing to modify it")


def validate_snapshot(client: QdrantClient, expected: set[str], profile_id: str):
    offset = None
    while True:
        points, offset = client.scroll(COLLECTION, limit=256, offset=offset,
                                       with_payload=["embedding_profile"], with_vectors=False)
        for point in points:
            if (str(point.id) not in expected or not point.payload
                    or point.payload.get("embedding_profile") != profile_id):
                raise ValueError("Collection contains different/stale data; refusing to mix snapshots or delete points")
        if offset is None:
            break


def index(root: Path) -> dict:
    profile = read_jsonl(root / "embedding-profile.jsonl")[0]
    if (profile["model"] != MODEL or profile["dimension"] != DIMENSION
            or profile["chunks_sha256"] != file_hash(root / "chunks.jsonl")
            or profile["vectors_sha256"] != file_hash(root / "vectors.jsonl")):
        raise ValueError("Artifacts changed or use a different model; run embed again")
    records = read_jsonl(root / "vectors.jsonl")
    expected = {record["id"] for record in records}
    if not records or len(expected) != len(records) or len(records) != profile["chunks"]:
        raise ValueError("Invalid or duplicate vector records")
    for record in records:
        validate_vector(record["vector"])
        if record["payload"]["embedding_profile"] != profile["profile_id"]:
            raise ValueError("Mixed embedding profiles")
    with closing(QdrantClient(url=QDRANT_URL, timeout=60)) as client:
        validate_collection(client, create=True)
        validate_snapshot(client, expected, profile["profile_id"])
        for start in range(0, len(records), 64):
            client.upsert(COLLECTION, wait=True, points=[models.PointStruct(
                id=record["id"], vector={VECTOR_NAME: record["vector"]}, payload=record["payload"])
                for record in records[start:start + 64]])
        count = client.count(COLLECTION, exact=True).count
        if count != len(records):
            raise RuntimeError("Indexed count does not match the local snapshot")
    return {"collection": COLLECTION, "points": count, "url": QDRANT_URL}


def search(root: Path, query: str, top_k: int = 3) -> list[dict]:
    if not query.strip() or len(query) > 2000 or not 1 <= top_k <= 20:
        raise ValueError("Use a nonempty query up to 2,000 characters and top_k of 1–20")
    profile = read_jsonl(root / "embedding-profile.jsonl")[0]
    encoder = LocalEncoder()
    if encoder.profile_id != profile["profile_id"]:
        raise ValueError("Local model changed; cannot query vectors from a different embedding profile")
    vector = next(encoder.vectors([query]))
    with closing(QdrantClient(url=QDRANT_URL, timeout=60)) as client:
        validate_collection(client)
        points = client.query_points(COLLECTION, query=vector, using=VECTOR_NAME, limit=top_k,
            query_filter=models.Filter(must=[models.FieldCondition(key="embedding_profile",
                match=models.MatchValue(value=encoder.profile_id))]), with_payload=True).points
    return [{"id": str(point.id), "score": point.score,
             "title": point.payload["title"], "page": point.payload["page"],
             "page_end": point.payload["page_end"], "page_labels": point.payload["page_labels"],
             "source_url": point.payload["source_url"], "text": point.payload["text"]}
            for point in points]
