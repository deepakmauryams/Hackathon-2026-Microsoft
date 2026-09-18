# Step 2: Native CAG scraper (no Docker)

Prerequisite: native `cag-qdrant` is running on `127.0.0.1:6333`. Keep `/data/files`
mounted persistently. The installer requires Ubuntu/Debian, Python 3.11+, sudo,
internet access to apt/PyPI, more than 8 GiB free on the data mount, and more than
3 GiB free on the /opt filesystem for Python dependencies. First
ingestion also needs HTTPS access to CAG and the model download host(s).

No API, Nginx, Qdrant configuration, firewall, block device or existing data is
changed. The installer does not start crawling. No Git commit/push is required
for the source-bundle transfer below.

## 1. Package and transfer from local PowerShell

Run from the repository root:

```powershell
python ingestion/deploy/package-native-worker.py
scp ingestion/.cache/cag-native-worker.tar.gz azureuser@20.81.233.151:~/
```

The bundle includes only ingestion Python source, requirements and the native
worker deployment files. It excludes PDFs, SQLite, vectors, local models, virtual
environments, API files and Docker configuration. Text is normalized to LF.

## 2. Install on the VM

```bash
mkdir -p ~/cag-native-source
tar -xzf ~/cag-native-worker.tar.gz -C ~/cag-native-source
bash ~/cag-native-source/ingestion/deploy/setup-scraper-native.sh
```

Run as `azureuser`, not `sudo bash`. It installs Poppler, Tesseract with English
and Hindi, an isolated Python venv, a non-login `cag-scraper` account, and a systemd
unit. Python packages are installed as the restricted worker, not root. The venv
is then made root-owned. TLS verification remains enabled. The worker service is
**installed but not started or enabled**. Qdrant remains running unchanged.

This is a fresh-install script, not an updater. It refuses existing target
paths/accounts rather than adopting or deleting them. If an installation fails
after creating its account/directories, retain the error and inspect the partial
installation; do not delete state or rerun blindly. Do not upgrade packages in
place after indexing: the embedding profile includes model/runtime versions.

## 3. Validate one real cycle

```bash
cag-scraper check
cag-scraper run --once
cag-scraper status
```

`check` validates the mount, writable directories, disk headroom, OCR language
packs, and Qdrant readiness/version. It does not fetch the model or test CAG access.
`run --once` downloads the multilingual model on demand and attempts one listing,
report-detail and PDF-processing cycle. It may take several minutes, especially
for OCR. Keep this SSH session open until it finishes. Manual CLI runs do not
inherit the systemd CPU/memory caps; those apply to the background service.

Check `documents`, `coverage`, `errors`, `worker_error` and `discovery_error` in the
status output. Expect at least one `indexed` or `partial` document and nonzero
chunks. `partial` means some pages remain low-text or failed OCR; blank pages can
also be low-text. A zero exit code alone is not proof that a document was indexed.
The initial collection is created lazily as `cag_catalogue_v1`, separate from the
older local sample `budget_document_chunks`.

## 4. Run continuously in the background

After inspecting the one-cycle result:

```bash
sudo systemctl enable --now cag-scraper
sudo systemctl status cag-scraper --no-pager
sudo journalctl -u cag-scraper -n 80 --no-pager
cag-scraper status
```

It continues after SSH disconnect and resumes SQLite checkpoints after restart.
Both Qdrant and the worker require the mount at boot. Worker limits: 4 GiB RAM,
no swap, two CPUs, low CPU/I/O priority. The service can be active while the
application is paused/backing off; always inspect catalogue status as well.

```bash
# Live logs (Ctrl+C exits viewing, not the worker):
sudo journalctl -u cag-scraper -f
# Categories/jurisdictions actually indexed:
cag-scraper facets
# Stop or resume without deleting progress:
sudo systemctl stop cag-scraper
sudo systemctl start cag-scraper
# Retry exhausted failures only after stopping the background worker:
sudo systemctl stop cag-scraper
cag-scraper retry-failed
sudo systemctl start cag-scraper
```

Never run a second worker concurrently. The OS worker lock rejects competing
writers. Status and facets can be inspected while the worker runs. Setup keeps
configuration shared between systemd and the CLI so they use the same data/model
profile. Logs use the system journal's existing retention settings; the unit
rate-limits messages but does not change global journal retention or add backups.

## Storage and coverage

| Purpose | VM location |
| --- | --- |
| Root-owned code and venv | `/opt/cag-scraper` |
| Shared environment | `/etc/cag-scraper/worker.env` |
| SQLite checkpoints and per-document work | `/data/files/cag/worker-native/pipeline` |
| Model/tokenizer caches | `/data/files/cag/worker-native/models` |
| Python/utility temporary files | `/data/files/cag/worker-native/tmp` |
| Existing Qdrant data, unchanged | `/data/files/cag/qdrant-native` |

- Crawl the public English and Hindi **audit report catalogues**, follow report
  detail pages and full PDFs (chapter fallback when no full PDF is found).
  This is not every CAG website/subdomain/attachment.
- Obey robots/access restrictions and use at least two seconds between requests.
- Preserve official sectors/types and source URLs; normalize jurisdiction/language
  metadata and mark derived categories separately from official taxonomy.
- Extract page-aware text, OCR low-text pages with `eng+hin`, chunk and embed
  locally using the existing 384-dimensional multilingual window-pooled encoder.
  Multilingual retrieval accuracy is not comprehensively evaluated.
- Publish complete document batches using `ready=true`; record partial extraction,
  errors and retry exhaustion. Successful PDFs/intermediate text are removed to
  save space; chunk text stays in Qdrant and coverage/metadata in SQLite.
- Keep 5 GiB reserve plus 1 GiB headroom before processing a PDF. The 30 GB mount
  **may not fit the entire catalogue**. Low space pauses work; independent Qdrant
  compaction and model downloads are not strictly bounded by the worker reserve.
- Limits: 500 MiB/PDF, 2,000 pages/PDF, extraction/text/time limits, English/Hindi
  OCR only. Inspect failures/partial results rather than assuming full coverage.
- Daily listing rescans discover new IDs. Already-known report IDs and replaced
  PDFs at the same URL are not automatically refreshed. Content deduplication
  uses the first indexed copy's metadata; aliases do not add every possible facet.

The native installer and Python tests are checked locally. Linux apt/model
downloads, actual OCR and the worker's systemd startup must be verified on the VM.
Do not run the old Docker worker alongside this native installation.