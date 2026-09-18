# Step 1: Qdrant only, without Docker

Run on Ubuntu/Debian with systemd, as the normal VM user with sudo privileges.
The mounted `/data/files` filesystem must already exist with more than 5 GiB free.
This script never formats, mounts or changes the block device. Ensure the mount
is persistent across reboots in your administrator-managed mount configuration.

From a checkout containing these files:

```bash
bash ingestion/deploy/setup-qdrant-native.sh
```

The installer fetches Qdrant 1.19.0's official static Linux release, checks its
SHA-256, installs it under `/opt/cag-qdrant`, and starts `cag-qdrant.service` with
a dedicated non-login user. Supported architectures: x86_64 and aarch64.
It installs only ca-certificates/curl via apt; Qdrant is not fetched from the
default distribution repositories. Configuration is `/etc/cag-qdrant/config.yaml`.

- HTTP: **127.0.0.1:6333 only**. gRPC and distributed mode are disabled.
- Data: **/data/files/cag/qdrant-native** (storage, snapshots, temporary files).
- Memory cap: 2 GiB; CPU cap: two CPUs. Logs: system journal on its existing disk.
- systemd requires the mount before startup and restarts Qdrant after failure.
- No API, scraper, collection, model download or Docker changes.
- Empty collections are expected until ingestion is configured separately.
- Raw binary archives do not necessarily include the web dashboard assets;
  verify with the HTTP endpoints below rather than depending on `/dashboard`.

```bash
sudo systemctl status cag-qdrant --no-pager
curl --fail http://127.0.0.1:6333/readyz
curl --fail http://127.0.0.1:6333/collections
sudo journalctl -u cag-qdrant -n 50 --no-pager
```

This is a **fresh-install script**, not a migration or upgrade tool. It refuses
existing target files/accounts and occupied port 6333 instead of adopting or
deleting them. If setup fails after creating files, inspect logs and the partial
installation; do not delete data or rerun blindly. For an installed service:

```bash
sudo systemctl restart cag-qdrant
# Intentional shutdown:
sudo systemctl stop cag-qdrant
```

Do not run the Docker setup alongside this native service: both use port 6333.
The native data directory is intentionally separate from Docker storage; there
is no automatic migration. Next: [install the native scraper](NATIVE-SCRAPER.md)
using the existing Qdrant service, without Docker.

No authentication is enabled because access is loopback-only. Local processes
can still access it. Do not expose 6333 through Azure firewall rules or Nginx.
There are no automatic backups or ongoing disk-reserve enforcement in this
Qdrant-only service. Monitor the 30 GB volume; the later worker enforces its own
reserve, which cannot constrain independent database compaction.

Verification performed locally: Bash syntax and actual x86_64 release archive
checksum/layout. The user verified native systemd startup on the VM on 2026-09-17:
Qdrant 1.19.0, readiness succeeded, and the collections list was empty.

References: [official release and SHA-256 digests](https://github.com/qdrant/qdrant/releases/expanded_assets/v1.19.0),
[official configuration](https://github.com/qdrant/qdrant/blob/v1.19.0/config/config.yaml).