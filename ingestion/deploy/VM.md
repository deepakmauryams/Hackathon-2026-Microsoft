# Single-VM CAG catalogue

**Native Qdrant first (no Docker):** use [NATIVE-QDRANT.md](NATIVE-QDRANT.md)
and [setup-qdrant-native.sh](setup-qdrant-native.sh), then the
[native scraper guide](NATIVE-SCRAPER.md). The rest of this guide
describes the Docker-based full worker. Do not run both Qdrant setups on port 6333.

## Current validation status

Python catalogue ingestion and filtered search were tested locally against the
real Qdrant server: one Manipur report produced 116 searchable chunks in the new
collection. The existing 837-point demo collection was preserved. Offline tests
cover metadata, checkpoints, retries, model windows, filters and disk guards.
The Linux image build was attempted twice but blocked by a local Docker-network
TLS handshake failure downloading packages from files.pythonhosted.org. Therefore
the full Linux/OCR runtime is **not yet validated**; setup fails if its image build
fails. Do not disable TLS verification to work around a proxy/network problem.
No VM deployment or repository push has been performed by the assistant.

Deploys the implemented `ingestion.catalogue` worker from this checkout. No API
integration, repository cloning, Git operations, disk formatting, automatic
mounting or host Python installation. Host package installation is opt-in only.

## Prerequisites and setup

- Linux, Bash, sudo, coreutils, `mountpoint` (util-linux), `ss` (iproute2).
- Existing local **rootful Docker Engine without user-namespace remapping**, and
  Docker Compose v2 with `bind.create_host_path` support. Follow the
  [official Engine installation guide](https://docs.docker.com/engine/install/)
  and [Compose plugin guide](https://docs.docker.com/compose/install/linux/).
  Alternatively, use `--install-docker` below when Docker is absent. Setup preserves
  existing Docker installations and enables/starts the Docker systemd service
  where available. It falls back to `sudo docker` for local engine access; it never
  adds users to the root-equivalent Docker group. A missing Compose plugin on an
  existing engine requires manual installation, system-wide for sudo access;
  setup will not replace the engine, even with the flag.
- Administrator-provisioned **30 GB data filesystem already mounted at
  `/data/files`**, with **more than 5 GiB free**. Setup checks mountpoint and disk
  space before every package operation and before creating data directories, and
  never prepares a block device or mounts anything. The worker needs an additional
  **1 GiB working headroom** above the **5 GiB reserve** to process a PDF. The
  reserve is conservative relative to 5 decimal GB.
- Make the mount persistent and order Docker startup **after this mount** using
  administrator-managed boot configuration (for example a Docker systemd drop-in
  with `RequiresMountsFor=/data/files`). This script does not change boot mounts or
  global Docker dependencies. Do not unmount while containers run. Missing bind
  directories fail closed, but that alone is not a boot-time mount dependency.
- Allow at least 6 GiB container RAM **plus OS/API headroom**, two worker CPUs,
  outbound HTTPS for package/image/model/CAG downloads. Docker images/build cache
  and bounded Docker logs remain in Docker's existing data-root, often the OS
  disk: provision free space there too. The 30 GB data volume is not guaranteed
  to fit the whole catalogue.

Run from the repository checkout, as the normal VM login user (not `sudo bash`).
For a VM without Docker:

```bash
bash ingestion/deploy/setup.sh --install-docker
```

The optional installer supports **Ubuntu jammy/noble/resolute** and **Debian
bookworm/trixie** only. It installs Docker Engine, Buildx and Compose from the
official `https://download.docker.com` apt repository with a dedicated `Signed-By`
keyring. It obtains curl/CA certificates through apt, downloads the key with
TLS verification, and never pipes a download into a shell or disables trust.
Existing conflicting Docker packages, `containerd`/`runc` or unmanaged runtimes
cause refusal; nothing is automatically removed. Existing Docker apt sources or
target keyring/source files are not overwritten, including unrelated content.
An interrupted installation may require administrator review before retrying.
Existing engines skip the installer regardless of OS; unsupported new installs
must be handled manually. No Docker data-root or user-group changes are made.

With Docker already installed, the default remains **no package installation**:

```bash
bash ingestion/deploy/setup.sh
```

Setup resolves its own location, checks port ownership without stopping anything,
builds the worker, checks Qdrant `/readyz` using the worker image's Python, then
starts the worker detached. It does not rely on Qdrant's image HEALTHCHECK.
Repeated setup updates this same `cag-catalogue` Compose project. A different
checkout using the same project name, an unrelated container (including the sample
Qdrant), or an unidentified host listener on 6333 causes refusal. Resolve conflicts
manually; no automatic migration/deletion. Avoid simultaneous setup invocations.

**Build/network limitation:** the current online Linux build is blocked by a
Docker TLS handshake failure reaching `files.pythonhosted.org` through the
corporate network. Resolve approved proxy/CA trust or use an approved network;
do not disable TLS verification or use pip trusted-host/insecure-download flags.
The Dockerfile is unchanged; a successful online build is not yet verified.

## Storage and isolation

| Host path | Purpose / ownership for newly created directories |
| --- | --- |
| `/data/files/cag` | Dedicated root; root:root, 0755 |
| `/data/files/cag/pipeline` | SQLite checkpoints/metadata, model profile and temporary PDF/extraction work; 10001:10001, 0700 |
| `/data/files/cag/models` | Model, Hugging Face and tokenizer caches; 10001:10001, 0700 |
| `/data/files/cag/qdrant` | Qdrant storage including snapshot directory; root:root, 0700 |

Existing directories must already have those owner/mode values; setup never
recursively changes ownership, adopts user files, or modifies other data paths.
Qdrant v1.19.0's [official Dockerfile](https://github.com/qdrant/qdrant/blob/v1.19.0/Dockerfile)
defaults to UID/GID 0:0; setup also verifies the pulled image's configured user.
Worker runs as fixed UID/GID 10001 with a read-only root filesystem and read-only
data root, writable pipeline/models submounts, a 512 MiB temporary filesystem,
no capabilities and **no Docker socket**. Temporary OCR files in `/tmp` are not
persistent. OCR packages: Poppler, Tesseract English and Hindi; additional language
packs require an explicit image update. Language availability is not OCR quality.

Qdrant v1.19.0: **2 GiB RAM**, HTTP published only on **127.0.0.1:6333**, no
published gRPC port. Worker: **4 GiB RAM, 2 CPUs**, low-thread environment, no
published port. Both have `unless-stopped` restart and 3 x 10 MB rotated logs per
container. Internal service discovery uses `http://qdrant:6333`; the bridge permits
outbound downloads (it is not an egress-isolated `internal: true` network).
Qdrant is unauthenticated: other local users/containers on the network can reach
it. Do not expose through public firewall rules, a reverse proxy, or the API.

## CLI and operation

The worker scans both public English and Hindi audit-report listings to the last
pagination link. It fetches official report details, keeps all identified full
PDF editions, and falls back to chapter PDFs when a full report is unavailable.
This is not a mirror of every CAG webpage, subdomain or arbitrary attachment.
Unsupported/broken links, inaccessible files, OCR gaps and exhausted retries mean
coverage is partial, not silently complete. Official types/sectors can be missing;
unknown values are retained and `state-finances` and other title-derived categories
are labelled separately from CAG's official classifications. State icon identifiers
normalize English/Hindi state names. Published year is distinct from audit period.

Each processing cycle discovers one listing page, resolves one queued report and
indexes one queued PDF. The first pass can take days. Daily listing rescans find
new report IDs but **do not automatically refresh already indexed PDFs at the same
URL or re-fetch details of previously resolved report IDs**. Explicit versioned
refresh/migration is a future feature. Language is inferred from link labels,
filenames or catalogue locale, not a language-detection guarantee. English/Hindi
OCR only covers low-text pages, not every scanned chart inside a text-rich page.

The multilingual model supports shorter inputs than the 650-token chunks. Each
chunk is split into model-sized windows without dropping text, and normalized
weighted mean embeddings are stored. This makes retrieval possible across supported
languages, but ranking quality has not been comprehensively evaluated. Full report
text is stored as chunk payloads, not as PDF/image binaries.

Completed documents publish with `ready=true`; search excludes incomplete uploads.
After success, temporary PDFs and page/chunk exports are removed to conserve the
30 GB volume; coverage summaries and source URLs remain in SQLite. Failed temporary
files are also removed before retry. Content hashes deduplicate exact PDFs; metadata
for duplicate source entries remains in SQLite, while filters use the first indexed
copy's metadata. Do not infer full metadata coverage from deduplicated point counts.

`status` reports indexed/partial/pending/failed documents, current pagination,
heartbeat, free disk bytes and aggregate OCR/low-text counts. `facets` lists actual
filter values and document counts for indexed/partial documents. Available filters:
jurisdiction, category, sector, language and published year.

From the repo root; no host Python dependencies or Docker group membership needed.
Commands below use sudo against the default local rootful engine, as setup requires:

```bash
sudo docker compose -f ingestion/deploy/compose.yaml ps
sudo docker compose -f ingestion/deploy/compose.yaml logs --tail 100 -f worker
sudo docker compose -f ingestion/deploy/compose.yaml exec worker python -m ingestion.catalogue --data-dir /data/files/cag/pipeline status
sudo docker compose -f ingestion/deploy/compose.yaml exec worker python -m ingestion.catalogue --data-dir /data/files/cag/pipeline facets
sudo docker compose -f ingestion/deploy/compose.yaml exec worker python -m ingestion.catalogue --data-dir /data/files/cag/pipeline search "fiscal deficit and public debt" --jurisdiction nagaland --category state-finances
# Explicit readiness probe (HTTP health, not ingestion completeness):
sudo docker compose -f ingestion/deploy/compose.yaml exec worker python ingestion/deploy/worker-entrypoint.py --check-qdrant
# Intentional stop persists across daemon restarts; no competing systemd worker unit:
sudo docker compose -f ingestion/deploy/compose.yaml stop
# Resume via setup for mount, space, port and readiness checks (also rebuilds):
bash ingestion/deploy/setup.sh
```

After correcting an exhausted failure, explicitly requeue it **with the worker
stopped** (leave Qdrant running). Never run a second mutating worker:

```bash
sudo docker compose -f ingestion/deploy/compose.yaml stop worker
sudo docker compose -f ingestion/deploy/compose.yaml run --rm --no-deps --entrypoint python worker -m ingestion.catalogue --data-dir /data/files/cag/pipeline retry-failed
bash ingestion/deploy/setup.sh
```

`unless-stopped` restarts crashed containers; explicit `stop` remains stopped
until setup/`up`/`start` is deliberately run. Container restarts on reboot require
Docker and the data mount prerequisites above. `down` removes containers/network,
not bind-mounted data. Temporary PDFs and extraction work are removed after both
successful and failed document attempts; retries download again. There is no
automatic backup or general Docker/data cleanup. Back up both the checkpoint tree
and Qdrant consistently before destructive maintenance.
This is **one node, not a high-availability cluster**.

## Implemented catalogue behavior

- Continuous `python -m ingestion.catalogue --data-dir PATH run` traverses the
  full **English and Hindi (`en` + `hi`) catalogue listings**, with resumable
  pagination and daily rescans. Discovery completion is not ingestion completion
  or a guarantee of website-wide coverage.
- PDF text extraction falls back to bounded Poppler/Tesseract **English + Hindi
  OCR (`eng+hin`)** on low-text pages. Partial coverage and OCR failures are
  recorded. Image-only tables on otherwise text-rich pages may be missed;
  extraction status is not a guarantee of complete or accurate table capture.
- FastEmbed/ONNX uses `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`:
  **384-dimensional multilingual vectors**, with token-weighted, normalized
  pooling across bounded text windows rather than truncating long chunks.
  Model/runtime profiles prevent mixing incompatible embeddings. The separate
  collection is **`cag_catalogue_v1`**, never sample `budget_document_chunks`.
- Compose configures `QDRANT_URL`, `CAG_COLLECTION`, `CAG_MODEL_CACHE`, `HF_HOME`,
  `TIKTOKEN_CACHE_DIR` and `CAG_THREADS=2`; the encoder explicitly passes its
  cache and thread settings to FastEmbed.
- `CAG_MIN_FREE_BYTES=5368709120` preserves a **5 GiB reserve** on the shared data
  filesystem. Document processing requires **another 1 GiB working headroom**;
  extraction and vector batches recheck the reserve. Disk pauses preserve queued
  work and retry. This is **not a quota** or a hard bound on downloads, model
  caching, Qdrant compaction or other writers. Do not reduce the reserve.
- SQLite checkpoints, an exclusive worker lock, retry backoff and recovery of
  interrupted documents support resumable work. Search only returns ready points
  matching the embedding profile. Exhausted failures require manual `retry-failed`
  after stopping the worker; indexed documents are not automatically requeued.
  SIGTERM requests shutdown with a two-minute container grace period; longer
  in-flight work may be killed and recovered on restart.
- `status` exposes counts, pagination/checkpoint state, heartbeat, errors and free
  disk space. `facets` and filtered `search` are separate query commands, not
  indexing loops. A running container or HTTP 200 proves neither crawl completion
  nor healthy progress; inspect status and logs. Search needs indexed data.

The entrypoint guards startup space and Qdrant readiness; the implemented
catalogue loop handles subsequent errors, disk pauses and retries. Runtime in
days is **unknown**: report count, network limits, OCR cost and available disk
dominate. Monitor progress before estimating it.