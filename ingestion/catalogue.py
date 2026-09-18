"""Resumable full-catalogue worker and filtered Python search, separate from API."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time

from bs4 import BeautifulSoup
from qdrant_client import QdrantClient, models

from ingestion.catalogue_encoder import CatalogueEncoder, DIMENSION, VECTOR_NAME
from ingestion.catalogue_meta import listing_reports, enrich_report, document_metadata, slug
from ingestion.catalogue_pdf import reserve
from ingestion.crawler import CagClient, START_URL, next_listing
from ingestion.io import read_jsonl, write_jsonl

LOG = logging.getLogger(__name__)
COLLECTION = os.environ.get("CAG_COLLECTION", "cag_catalogue_v1")
URL = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
FILTERS = ("source", "jurisdiction", "categories", "sectors", "government_type", "language", "document_id", "embedding_profile")


def connect(root):
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "catalogue.sqlite", timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript('''PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reports(id TEXT PRIMARY KEY, metadata TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            retry_at REAL NOT NULL DEFAULT 0, error TEXT);
        CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, metadata TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            retry_at REAL NOT NULL DEFAULT 0, error TEXT, sha256 TEXT, summary TEXT);
        CREATE INDEX IF NOT EXISTS doc_hash ON documents(sha256);
        CREATE INDEX IF NOT EXISTS doc_queue ON documents(status,retry_at);
    ''')
    return db


def put_state(db, key, value):
    with db:
        db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))


def state(db, key, default=None):
    row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def due(db, table):
    assert table in ("reports", "documents")
    return db.execute(f"SELECT * FROM {table} WHERE status='pending' AND retry_at<=? ORDER BY rowid LIMIT 1", (time.time(),)).fetchone()


def failed(db, table, row, error):
    attempts = row["attempts"] + 1
    with db:
        db.execute(f"UPDATE {table} SET status=?,attempts=?,retry_at=?,error=? WHERE id=?",
            ("failed" if attempts >= 5 else "pending", attempts, time.time() + min(86400, 60 * 2**attempts),
             str(error)[:1500], row["id"]))


@contextmanager
def worker_lock(root):
    """OS-owned lock released even on crash; CLI status/search remain read-only."""
    with (root / "worker.lock").open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.write(b"0"); stream.flush(); stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def discover(db, client):
    url = state(db, "next_listing", START_URL)
    if url is None:
        # Daily complete rescan discovers additions shifted across page boundaries.
        if time.time() - state(db, "catalogue_completed_at", 0) < 86400:
            return False
        url = START_URL
        put_state(db, "pages_this_pass", 0)
    html = client.small_text(client.get(url))
    reports = listing_reports(html, url)
    next_url = next_listing(html, url)
    # Refuse premature completion if pagination still advertises later pages.
    from urllib.parse import parse_qs, urlsplit
    current = int(parse_qs(urlsplit(url).query).get("page", ["1"])[0])
    advertised = []
    for a in BeautifulSoup(html, "html.parser").select('a[href*="page="]'):
        try:
            advertised.append(int(parse_qs(urlsplit(a["href"]).query)["page"][0]))
        except (ValueError, KeyError):
            pass
    if next_url is None and any(n > current for n in advertised):
        raise ValueError("Pagination has later pages but no next link; refusing silent truncation")
    next_target = "https://cag.gov.in/hi/audit-report" if next_url is None and "/en/" in url else next_url
    with db:
        for report in reports:
            db.execute("INSERT OR IGNORE INTO reports(id,metadata) VALUES (?,?)", (report["report_id"], json.dumps(report)))
        db.execute("INSERT OR REPLACE INTO state VALUES ('next_listing',?)", (json.dumps(next_target),))
    put_state(db, "pages_this_pass", state(db, "pages_this_pass", 0) + 1)
    if next_url is None:
        if "/hi/" in url:
            put_state(db, "catalogue_completed_at", time.time())
    return True


def detail(db, client, row):
    report = json.loads(row["metadata"])
    report = enrich_report(report, client.small_text(client.get(report["report_url"])))
    with db:
        for pdf in report["pdfs"]:
            doc = document_metadata(report, pdf)
            db.execute("INSERT OR IGNORE INTO documents(id,metadata) VALUES (?,?)", (doc["document_id"], json.dumps(doc)))
        db.execute("UPDATE reports SET status='discovered',metadata=?,error=NULL WHERE id=?", (json.dumps(report), row["id"]))


def ensure_collection(client, profile_id):
    if not client.collection_exists(COLLECTION):
        client.create_collection(COLLECTION, vectors_config={VECTOR_NAME: models.VectorParams(
            size=DIMENSION, distance=models.Distance.COSINE, on_disk=True)}, on_disk_payload=True)
    config = client.get_collection(COLLECTION).config.params.vectors
    if (not isinstance(config, dict) or set(config) != {VECTOR_NAME}
            or config[VECTOR_NAME].size != DIMENSION or config[VECTOR_NAME].distance != models.Distance.COSINE):
        raise ValueError("Existing collection is not the expected catalogue schema")
    mismatched = client.count(COLLECTION, count_filter=models.Filter(must_not=[models.FieldCondition(
        key="embedding_profile", match=models.MatchValue(value=profile_id))]), exact=True).count
    if mismatched:
        raise ValueError("Collection has vectors from another model/runtime; use a new collection, never mix")
    schema = client.get_collection(COLLECTION).payload_schema
    for key in FILTERS:
        if key not in schema:
            client.create_payload_index(COLLECTION, key, models.PayloadSchemaType.KEYWORD, wait=True)
    for key, kind in (("published_year", models.PayloadSchemaType.INTEGER), ("ready", models.PayloadSchemaType.BOOL)):
        if key not in schema:
            client.create_payload_index(COLLECTION, key, kind, wait=True)


def process_document(db, root, client, qdrant, encoder, row):
    reserve(root)
    # Budget for one PDF plus OCR/extraction working files above the fixed reserve.
    if shutil.disk_usage(root).free < int(os.environ.get("CAG_MIN_FREE_BYTES", str(5 * 1024**3))) + 1024**3:
        raise OSError("Low disk space: need reserve plus 1 GiB working headroom")
    doc = json.loads(row["metadata"])
    work = root / "work" / row["id"]
    work.mkdir(parents=True, exist_ok=True)
    with db:
        db.execute("UPDATE documents SET status='processing' WHERE id=?", (row["id"],))
    LOG.info("Processing document %s: %s", row["id"], doc.get("title", ""))
    downloaded = client.download(doc, work, 500 * 1024**2)
    doc.update(downloaded)
    duplicate = db.execute("SELECT id FROM documents WHERE sha256=? AND status IN ('indexed','partial') AND id<>?",
                           (doc["sha256"], row["id"])).fetchone()
    if duplicate:
        with db:
            db.execute("UPDATE documents SET status='duplicate',sha256=?,summary=? WHERE id=?",
                       (doc["sha256"], json.dumps({"duplicate_of": duplicate["id"]}), row["id"]))
        shutil.rmtree(work)
        return
    (work / "document.json").write_text(json.dumps(doc), encoding="utf-8")
    # A pathological PDF cannot retain memory in the long-lived worker process.
    subprocess.run([sys.executable, "-m", "ingestion.catalogue_pdf", str(work)], check=True, timeout=7200)
    summary = read_jsonl(work / "summary.jsonl")[0]
    chunks = read_jsonl(work / "chunks.jsonl")
    doc_filter = models.Filter(must=[models.FieldCondition(key="document_id", match=models.MatchValue(value=row["id"]))])
    # Only this queued document's incomplete points may be replaced on a retry.
    # Indexed documents are never requeued automatically or deleted by rescans.
    qdrant.delete(COLLECTION, points_selector=models.FilterSelector(filter=doc_filter), wait=True)
    for start in range(0, len(chunks), 8):
        reserve(root)
        points = []
        for chunk in chunks[start:start + 8]:
            payload = {**doc, **chunk, "embedding_profile": encoder.profile_id, "embedding_model": encoder.profile["model"],
                       "ready": False, "coverage": summary["coverage"]}
            for key in ("pdf_path", "bytes"):
                payload.pop(key, None)
            points.append(models.PointStruct(id=chunk["id"], vector={VECTOR_NAME: encoder.vector(chunk["text"])}, payload=payload))
        qdrant.upsert(COLLECTION, points=points, wait=True)
        put_state(db, "heartbeat", {"at": time.time(), "stage": "embedding", "document_id": row["id"],
                                   "chunks_done": min(start + 8, len(chunks)), "chunks_total": len(chunks)})
    if qdrant.count(COLLECTION, count_filter=doc_filter, exact=True).count != len(chunks):
        raise ValueError("Document point count mismatch")
    qdrant.set_payload(COLLECTION, payload={"ready": True}, points=doc_filter, wait=True)
    with db:
        db.execute("UPDATE documents SET status=?,sha256=?,metadata=?,summary=?,error=NULL WHERE id=?",
            ("partial" if summary["coverage"] == "partial" else "indexed", doc["sha256"], json.dumps(doc), json.dumps(summary), row["id"]))
    # Text lives in Qdrant; metadata and coverage live in SQLite. Don't retain all PDFs.
    shutil.rmtree(work)
    LOG.info("Indexed document %s: %s chunks, coverage=%s", row["id"], len(chunks), summary["coverage"])


def status(db, root):
    counts = {table: dict(db.execute(f"SELECT status,count(*) FROM {table} GROUP BY status").fetchall()) for table in ("reports", "documents")}
    errors = {table: [dict(r) for r in db.execute(f"SELECT id,status,attempts,error FROM {table} WHERE error IS NOT NULL LIMIT 20")] for table in ("reports", "documents")}
    coverage = {"pages": 0, "chunks": 0, "ocr_pages": 0, "low_text_pages": 0, "ocr_failures": 0}
    for row in db.execute("SELECT summary FROM documents WHERE status IN ('indexed','partial') AND summary IS NOT NULL"):
        summary = json.loads(row[0])
        for key in coverage:
            coverage[key] += len(summary.get(key, [])) if key == "ocr_failures" else summary.get(key, 0)
    return {**counts, "coverage": coverage, "pages_this_pass": state(db, "pages_this_pass", 0), "next_listing": state(db, "next_listing", START_URL),
        "catalogue_pass_completed_at": state(db, "catalogue_completed_at"), "heartbeat": state(db, "heartbeat"),
        "worker_error": state(db, "worker_error"), "free_bytes": shutil.disk_usage(root).free,
        "discovery_error": state(db, "discovery_error"),
        "collection": COLLECTION, "errors": errors,
        "note": "Catalogue discovery is not full ingestion. Partial/OCR failures require review; website-wide coverage is not guaranteed."}


def facets(db):
    from collections import Counter
    result = {key: Counter() for key in ("jurisdiction", "categories", "sectors", "language", "government_type", "published_year")}
    for row in db.execute("SELECT metadata FROM documents WHERE status IN ('indexed','partial')"):
        record = json.loads(row[0])
        for key, counter in result.items():
            values = record.get(key)
            for value in values if isinstance(values, list) else [values]:
                if value is not None:
                    counter[str(value)] += 1
    return {key: dict(sorted(value.items())) for key, value in result.items()}


def query_filter(profile_id, jurisdiction=None, category=None, sector=None, language=None, year=None):
    must = [models.FieldCondition(key="embedding_profile", match=models.MatchValue(value=profile_id)),
            models.FieldCondition(key="ready", match=models.MatchValue(value=True))]
    for key, value in (("jurisdiction", jurisdiction), ("categories", category), ("sectors", sector), ("language", language)):
        if value:
            must.append(models.FieldCondition(key=key, match=models.MatchValue(value=slug(value))))
    if year is not None:
        must.append(models.FieldCondition(key="published_year", match=models.MatchValue(value=year)))
    return models.Filter(must=must)


def run(root, db, once=False, max_documents=None):
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    encoder = qdrant = None
    processed = 0
    with worker_lock(root):
        with db:
            db.execute("UPDATE documents SET status='pending' WHERE status='processing'")
        client = CagClient(delay=2)
        robots_checked = 0
        try:
            while not stop.is_set():
                try:
                    reserve(root)
                    if time.time() - robots_checked > 3600:
                        client.check_robots()
                        robots_checked = time.time()
                    put_state(db, "heartbeat", {"at": time.time(), "stage": "discovery"})
                    if time.time() >= state(db, "discovery_retry_at", 0):
                        try:
                            discover(db, client)
                            put_state(db, "discovery_error", None)
                        except PermissionError:
                            raise
                        except Exception as error:
                            put_state(db, "discovery_error", str(error))
                            put_state(db, "discovery_retry_at", time.time() + 300)
                            LOG.exception("Catalogue page discovery failed; checkpoint preserved")
                    report = due(db, "reports")
                    if report:
                        try:
                            detail(db, client, report)
                        except PermissionError:
                            raise
                        except Exception as error:
                            failed(db, "reports", report, error)
                            LOG.exception("Report discovery failed")
                    row = due(db, "documents")
                    if row:
                        if encoder is None:
                            encoder = CatalogueEncoder()
                            previous = state(db, "embedding_profile")
                            if previous and previous != encoder.profile_id:
                                raise ValueError("Embedding model/runtime changed. Use a fresh data directory and collection.")
                            qdrant = QdrantClient(url=URL, timeout=120)
                            ensure_collection(qdrant, encoder.profile_id)
                            put_state(db, "embedding_profile", encoder.profile_id)
                            write_jsonl(root / "model-profile.jsonl", [{**encoder.profile, "profile_id": encoder.profile_id}])
                        try:
                            put_state(db, "heartbeat", {"at": time.time(), "stage": "extracting", "document_id": row["id"]})
                            process_document(db, root, client, qdrant, encoder, row)
                            processed += 1
                        except PermissionError:
                            with db:
                                db.execute("UPDATE documents SET status='pending' WHERE id=?", (row["id"],))
                            raise
                        except OSError as error:
                            if "disk space" in str(error):
                                with db:
                                    db.execute("UPDATE documents SET status='pending' WHERE id=?", (row["id"],))
                                raise
                            failed(db, "documents", row, error)
                        except Exception as error:
                            failed(db, "documents", row, error)
                            LOG.exception("PDF processing failed")
                            if once:
                                raise
                        finally:
                            # Failed PDFs can otherwise consume the entire 30GB volume.
                            work = root / "work" / row["id"]
                            if work.exists():
                                shutil.rmtree(work)
                    put_state(db, "worker_error", None)
                    put_state(db, "heartbeat", {"at": time.time(), "stage": "cycle-complete", "processed_this_run": processed})
                    if once or (max_documents and processed >= max_documents):
                        break
                    if not row and not report:
                        stop.wait(60)
                except Exception as error:
                    put_state(db, "worker_error", {"at": time.time(), "error": str(error)})
                    LOG.exception("Worker paused; progress retained")
                    if once:
                        raise
                    # Do not repeatedly download/init models on fatal profile failures.
                    encoder = None
                    if qdrant:
                        qdrant.close(); qdrant = None
                    stop.wait(300)
        finally:
            client.close()
            if qdrant:
                qdrant.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).parent / "catalogue-data")
    commands = parser.add_subparsers(dest="command", required=True)
    worker = commands.add_parser("run")
    worker.add_argument("--once", action="store_true", help="One listing/detail/document cycle for validation")
    worker.add_argument("--max-documents", type=int, help="Stop after this many successful documents")
    commands.add_parser("status")
    commands.add_parser("facets")
    commands.add_parser("retry-failed", help="Explicitly requeue exhausted failures, not indexed documents")
    search = commands.add_parser("search")
    search.add_argument("query")
    for field in ("jurisdiction", "category", "sector", "language"):
        search.add_argument("--" + field)
    search.add_argument("--year", type=int, help="Published year, NOT audit period")
    search.add_argument("--top-k", type=int, default=5, choices=range(1, 21))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.command != "run" and not (args.data_dir / "catalogue.sqlite").exists():
        parser.error("Catalogue does not exist yet; start the worker first")
    db = connect(args.data_dir)
    try:
        if args.command == "run":
            if args.max_documents is not None and args.max_documents < 1:
                parser.error("--max-documents must be positive")
            run(args.data_dir, db, args.once, args.max_documents)
            result = status(db, args.data_dir)
        elif args.command == "status":
            result = status(db, args.data_dir)
        elif args.command == "facets":
            result = facets(db)
        elif args.command == "retry-failed":
            with worker_lock(args.data_dir), db:
                for table in ("reports", "documents"):
                    db.execute(f"UPDATE {table} SET status='pending',attempts=0,retry_at=0 WHERE status='failed'")
            result = {"message": "Exhausted failures requeued; restart stopped worker"}
        else:
            if not args.query.strip() or len(args.query) > 2000:
                parser.error("Query must contain 1–2000 characters")
            encoder = CatalogueEncoder()
            if state(db, "embedding_profile") != encoder.profile_id:
                raise ValueError("Query model/profile differs from indexed model")
            qdrant = QdrantClient(url=URL, timeout=120)
            try:
                hits = qdrant.query_points(COLLECTION, using=VECTOR_NAME, query=encoder.vector(args.query),
                    query_filter=query_filter(encoder.profile_id, args.jurisdiction, args.category, args.sector, args.language, args.year),
                    limit=args.top_k, with_payload=True).points
                result = [{"score": hit.score, **hit.payload} for hit in hits]
            finally:
                qdrant.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()