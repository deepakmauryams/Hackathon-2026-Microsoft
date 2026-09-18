#!/usr/bin/env bash
# Shared service environment for checks, status, one-cycle validation and queries.
set -Eeuo pipefail
if [[ $(id -un) != cag-scraper ]]; then
    # Enter a worker-accessible directory before changing identity.
    cd /opt/cag-scraper/app
    exec sudo -u cag-scraper -- /usr/local/bin/cag-scraper "$@"
fi
set -a
. /etc/cag-scraper/worker.env
set +a
cd /opt/cag-scraper/app
if [[ ${1:-} == check && $# -eq 1 ]]; then
    exec /opt/cag-scraper/venv/bin/python ingestion/deploy/native-preflight.py
fi
exec /opt/cag-scraper/venv/bin/python -m ingestion.catalogue --data-dir "$CAG_DATA_DIR" "$@"