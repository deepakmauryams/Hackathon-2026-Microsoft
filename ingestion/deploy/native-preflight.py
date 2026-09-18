"""Read-only native worker readiness checks; no crawling or model downloads."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

import httpx


def check():
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11+ is required")
    if not os.path.ismount("/data/files"):
        raise RuntimeError("/data/files must be mounted; refusing root-disk fallback")
    root = Path(os.environ["CAG_DATA_DIR"])
    for key in ("CAG_DATA_DIR", "CAG_MODEL_CACHE", "TMPDIR"):
        directory = Path(os.environ[key])
        if not directory.is_dir() or not os.access(directory, os.W_OK | os.X_OK):
            raise RuntimeError(f"Worker needs an existing writable directory: {directory}")
    reserve = int(os.environ.get("CAG_MIN_FREE_BYTES", "5368709120"))
    if reserve < 5 * 1024**3:
        raise RuntimeError("Keep at least 5 GiB disk reserve")
    if shutil.disk_usage(root).free < reserve + 1024**3:
        raise RuntimeError("Low disk space: need reserve plus 1 GiB working headroom")
    for tool in ("pdftoppm", "tesseract"):
        if not shutil.which(tool):
            raise RuntimeError(f"Missing OCR tool: {tool}")
    languages = subprocess.run(["tesseract", "--list-langs"], check=True,
                               capture_output=True, text=True, timeout=30).stdout.splitlines()
    if not {"eng", "hin"}.issubset({line.strip() for line in languages}):
        raise RuntimeError("Tesseract requires both eng and hin language packs")
    url = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333").rstrip("/")
    if url != "http://127.0.0.1:6333":
        raise RuntimeError("Native worker expects the existing loopback Qdrant endpoint")
    with httpx.Client(trust_env=False, timeout=10) as client:
        client.get(f"{url}/readyz").raise_for_status()
        response = client.get(f"{url}/")
        response.raise_for_status()
        if not response.json().get("version", "").startswith("1.19."):
            raise RuntimeError("Expected Qdrant 1.19.x to match the pinned Python client")
    print("Preflight passed: mounted data, disk headroom, eng+hin OCR and Qdrant ready", flush=True)


if __name__ == "__main__":
    try:
        check()
    except Exception as error:
        print(f"Preflight failed: {error}", file=sys.stderr)
        raise SystemExit(1)