"""Startup guard only; the catalogue must enforce disk reserve during run."""

import os
import shutil
import sys
import time
import urllib.error
import urllib.request


def wait_for_qdrant() -> None:
    url = os.environ.get("QDRANT_URL", "http://qdrant:6333").rstrip("/")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(60):
        try:
            with opener.open(f"{url}/readyz", timeout=5) as response:
                if response.status == 200:
                    print("Qdrant ready", flush=True)
                    return
        except (OSError, urllib.error.URLError) as exc:
            print(f"Waiting for Qdrant ({attempt + 1}/60): {exc}", flush=True)
        time.sleep(2)
    raise SystemExit("Qdrant readiness timed out; no catalogue process started")


def main() -> None:
    if sys.argv[1:] == ["--check-qdrant"]:
        wait_for_qdrant()
        return
    if len(sys.argv) < 2:
        raise SystemExit("Expected a worker command")
    reserve = int(os.environ.get("CAG_MIN_FREE_BYTES", "5368709120"))
    if reserve < 5 * 1024**3:
        raise SystemExit("CAG_MIN_FREE_BYTES must be at least 5 GiB")
    while shutil.disk_usage("/data/files/cag/pipeline").free <= reserve:
        print("Disk pause: <= 5 GiB/configured reserve free; retrying in 60s", flush=True)
        time.sleep(60)
    wait_for_qdrant()
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()