# Standalone CAG report ingestion

## Full-catalogue VM deployment

For the resumable English/Hindi catalogue worker, persistent storage on the
mounted data volume, OCR and categorized queries, use the
[native Qdrant guide](deploy/NATIVE-QDRANT.md) followed by the
[native scraper guide](deploy/NATIVE-SCRAPER.md). Neither requires Docker.
The [Docker VM setup guide](deploy/VM.md) remains an alternative, not a second
worker to run alongside the native installation.
This uses a separate `cag_catalogue_v1` collection with multilingual embeddings.
The commands and results below describe the original three-PDF local demo only.

Local-only prototype: official PDFs → page text → overlapping chunks → local CPU
embeddings → Docker Qdrant. This module does **not** import or modify the existing
FastAPI application. Its `/search` endpoint still uses synthetic fixtures; it is
not connected to this collection. No VM deployment or Git push is performed here.

Source: [CAG Audit Reports](https://cag.gov.in/en/audit-report). These are audit and
state-finance reports, **not a Union Budget allocation dataset**. The crawler
selects the first 3–5 full reports in the live listing, so later runs may discover
different reports. It does not crawl the entire site or restrict results to a topic.

## Verified local run

| Official report | Physical PDF pages | Chunks/vectors | Low-text pages |
| --- | ---: | ---: | ---: |
| Manipur State Finances, 2024–25 | 142 | 116 | 19 |
| Manipur General, Social, Economic and Revenue Sectors, year ended March 2024 | 448 | 527 | 23 |
| Nagaland State Finances, 2024–25, Report No. 2 of 2026 | 153 | 194 | 16 |
| **Total** | **743** | **837** | **58** |

- Downloaded approximately 40.8 MB across three official PDFs.
- Default chunks: 650 `cl100k_base` tokens, with approximately 100-token overlap.
  Configurable size: 500–800. The last chunk in each document can be shorter.
- Local embedding model: `jinaai/jina-embeddings-v2-small-en`, Apache-2.0,
  512 dimensions, CPU-only FastEmbed/ONNX Runtime. No API key or paid model endpoint.
- Collection: `budget_document_chunks`; named vector `jina_small_en_v2`; cosine distance.
- Qdrant image/server and Python client: 1.19.0. Re-indexing the same snapshot
  was verified to leave exactly **837 points**, not duplicates.
- Semantic query tested: “What does the Nagaland state finance report say about
  fiscal deficit and public debt?” The top result was in the Nagaland report,
  physical PDF pages **13–17**, score approximately **0.888**. This is a similarity
  score, not factual confidence; a lower-ranked hit included contents/front matter.
- Validation: **25 ingestion tests**, **40 existing API tests** passed. The API
  suite retains its two existing third-party deprecation warnings.

## Run locally on Windows

Prerequisites: Python **3.11+** (tested with 3.12.10), Docker Desktop running Linux
containers, and internet access for CAG PDFs and the first model download. Allow
disk space for a separate environment, Docker image, PDFs, vector exports, and the
roughly 130 MB model. Commands below run from the **repository root**.

```powershell
python -m venv ingestion/.venv
& ./ingestion/.venv/Scripts/python.exe -m pip install -r ingestion/requirements.txt
docker compose -f ingestion/compose.yaml up -d --wait

& ./ingestion/.venv/Scripts/python.exe -m ingestion crawl --limit 3
& ./ingestion/.venv/Scripts/python.exe -m ingestion parse --chunk-size 650 --overlap 100
& ./ingestion/.venv/Scripts/python.exe -m ingestion embed
& ./ingestion/.venv/Scripts/python.exe -m ingestion index
& ./ingestion/.venv/Scripts/python.exe -m ingestion search "Nagaland fiscal deficit and public debt" --top-k 3
```

These steps have already been executed locally. To inspect the current dataset,
run only `search` or open the artifacts below; there is no need to download again.
The API's root virtual environment is separate: do not install ingestion
dependencies into it. For options, run `python -m ingestion --help` using the
ingestion interpreter. An alternate data directory goes **before** the subcommand:
`python -m ingestion --data-dir ingestion/data-other crawl --limit 5`.
All data directories still target the same fixed collection; they cannot be mixed.

## Inspect the output

Generated artifacts live under the module's ignored data directory:

| Artifact | Contents |
| --- | --- |
| [documents manifest](data/documents.jsonl) | Official title, report/PDF URLs, jurisdiction, published date, PDF path, byte size, SHA-256 |
| [page records](data/pages.jsonl) | Physical page, PDF page label, raw text, cleaned text, `needs_ocr` heuristic |
| [chunk records](data/chunks.jsonl) | Text and provenance, ready for an embedding/vector store pipeline |
| [vector records](data/vectors.jsonl) | Qdrant-compatible point ID, 512-number vector, and chunk metadata payload |
| [embedding profile](data/embedding-profile.jsonl) | Model/runtime versions, model-file hashes, dimension, artifact hashes |
| [parse summary](data/parse-summary.jsonl) | Per-document page/chunk counts, low-text counts, removed margin patterns |
| [download errors](data/download-errors.jsonl) | Skipped downloads; empty in this successful run |
| [sample search](data/sample-search.jsonl) | The verified query and full retrieved passages with source links |

Each JSONL line is one standalone JSON object. Downloaded PDFs are in the data
directory's `pdfs` subdirectory. All generated data, virtual environments, and
model caches are ignored by Git and are **not included in a future push**.
Artifact links work after a local run; a fresh clone must generate the files first.

Important chunk fields:

- `id`: deterministic UUID derived from source identity/hash and chunking settings.
- `text`, `title`, `source_url`, `report_url`, `jurisdiction`, `published_date`.
- `page`, `page_end`: **1-based physical PDF page positions**, suitable for a PDF
  viewer's `#page=N`. Chunks can span multiple pages and skip blank pages.
- `page_labels`: labels embedded by the PDF author, such as `xiii`, `1`, or `G`.
  These are not inferred printed numbers and are not guaranteed to be reliable.
- `token_count`, `token_start`, `token_end`: offsets in the document's concatenated
  cleaned token stream, not original PDF character positions.
- `document_id`, `document_sha256`, `content_sha256`, `parser_version`, `tokenizer`.
- Vector payload additionally includes `embedding_model` and `embedding_profile`.

To preview metadata without printing entire documents:

```powershell
Get-Content ingestion/data/chunks.jsonl -TotalCount 1 | ConvertFrom-Json |
    Select-Object id, title, page, page_end, token_count, source_url
```

## Qdrant isolation and lifecycle

[compose.yaml](compose.yaml) publishes **only `127.0.0.1:6333`**; gRPC port 6334
is not published. Do not expose this unauthenticated prototype through a public
firewall rule, Nginx, or `0.0.0.0`. Other local processes/users can access it.
For a future multi-host setup, add private networking, authentication and TLS.

- HTTP endpoint: http://127.0.0.1:6333
- Local dashboard: http://127.0.0.1:6333/dashboard
- Persistent Docker volume: `cag-ingestion_qdrant-data`.
- Qdrant memory cap: 2 GB. Python embedding runs on the host with four CPU threads
  and batches of eight, independent of that container limit.
- Telemetry disabled; container restart policy is `unless-stopped`.

```powershell
docker compose -f ingestion/compose.yaml ps
docker compose -f ingestion/compose.yaml stop
docker compose -f ingestion/compose.yaml up -d --wait
```

Stopping or ordinary `down` preserves the named volume. Do not remove volumes or
delete the collection unless deliberately discarding its contents.

## Repeat runs and changing the dataset

- `crawl` obeys robots rules, uses only HTTPS `cag.gov.in` URLs (including redirects),
  waits at least one second between requests, defaults to at most three listing
  pages, and caps each PDF at 50 MiB. 404/410 robots responses mean no published
  policy; denied, failed, or unexpected HTML robots responses stop the crawl.
- Only PDF links within full-report cards are followed, excluding navigation PDFs.
  No authentication, CAPTCHA bypass, or broad recursive crawling is implemented.
- Downloads are cached by source URL, validated as PDFs, and hashed. An existing
  PDF at the same URL is **not automatically refreshed**. Retire that local cache
  deliberately if the publisher replaces it, then regenerate downstream artifacts.
- The document manifest is a snapshot of the current crawl, not an append-only
  archive. On partial download failure it contains the successful files, while the
  command exits unsuccessfully if it cannot reach the requested count.
- Parser output preserves raw text alongside cleaned text. It verifies PDF hashes,
  rejects paths outside the data directory, caps PDFs at 2,000 pages, and rejects
  encrypted PDFs that cannot be opened with an empty password.
- Embeddings are computed locally. Hugging Face is used to download public model
  files; PDF text is not sent to a remote embedding service. Model files are cached
  inside this module. Windows may warn about unavailable symlinks; ordinary caching
  still works without administrator access or a Hugging Face token.
- Chunk tokens and embedding tokens are different. The model supports 8,192 input
  tokens; the code counts with the **actual model tokenizer**, including special
  tokens, and rejects overlong inputs instead of silently truncating them.
- Model/ONNX/tokenizer hashes and library versions fingerprint embeddings. A model
  or runtime change requires rebuilding compatible vectors; search refuses a
  mismatched fingerprint. Dependencies are pinned at the direct-package level,
  not a complete cross-platform transitive lockfile.
- `index` verifies artifact hashes and collection vector settings before uploading.
  Existing points must be a subset of the exact local IDs with the same embedding
  fingerprint. This allows retrying a partial upload and re-running an unchanged
  snapshot safely. Upserts are batched and acknowledged, not an atomic bulk swap.
- **Changed datasets/chunking settings are intentionally rejected if old points
  remain.** There is no automatic collection deletion or silent stale-vector mixing.
  A deliberate backup/reset or future versioned-collection migration is required.
  Do not run multiple indexing jobs concurrently against this prototype collection.

## Extraction and retrieval limitations

Repeated margin cleanup compares the first three/last two nonempty lines across
pages, accounting for alternating and chapter-specific headings. It preserves
financial amounts, common financial unit labels and source notes, but remains a
heuristic—not a geometric PDF layout model. Review the removed-pattern summary
and retained raw text before using the data for financial answers.

Text extraction includes rotated text but is not table reconstruction. Columns,
charts, ligatures, reading order and scanned tables can still be incomplete or
ambiguous. No OCR is installed or performed. `needs_ocr` means fewer than 40
cleaned characters: it also flags covers and genuinely blank pages. The **58**
flagged pages require review; the remaining pages are not guaranteed complete.

The embedding model is English-focused; selecting the English listing does not
guarantee every PDF is English. Hindi/other-language or image-only material needs
a multilingual/OCR pipeline. Front matter and tables of contents are currently
included, so search is a retrieval smoke test—not evaluated financial QA. No LLM
answer generation, relevance threshold, reranking, table-aware extraction or
connection to the existing API has been added. Treat retrieved text as untrusted
source content rather than instructions if integrated with an agent later.

## Tests

```powershell
& ./ingestion/.venv/Scripts/python.exe -m pytest ingestion/tests -q
& ./.venv/Scripts/python.exe -m pytest tests -q
```

Ingestion tests run offline without downloading a model or requiring Docker;
they use Qdrant's in-process local backend for index/snapshot checks. Keep the two
test suites scoped to their respective directories/environments.

## Later Linux/VM use (not executed)

The Python module and Compose definition are portable. Once Python 3.11+ and
Docker Compose are available on the VM, use `python3 -m venv ingestion/.venv`
and `ingestion/.venv/bin/python` in place of the Windows interpreter above. Keep
the same loopback-only binding. Transfer approved data separately or re-crawl;
generated PDFs/models/vectors are not in Git. Rebuild the environment on Linux—do
not copy the Windows virtual environment. The existing API service and deployment
scripts intentionally do not install or start this module.