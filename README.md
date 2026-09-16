# Hackathon-2026-Microsoft

A Python FastAPI starter for the hackathon, with tests and an Ubuntu/Debian VM
deployment using **Nginx + systemd**. No database, credentials, or cloud SDKs are
required. The demo endpoints are public and unauthenticated; add authentication
before exposing sensitive functionality.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | API welcome message |
| GET | `/health` | Liveness check: `{"status":"ok"}` |
| GET | `/api/hello?name=Deepak` | Example greeting; name length is 1–100 characters |
| GET | `/docs` | Interactive Swagger UI |
| GET | `/redoc` | Alternative API documentation |
| GET | `/openapi.json` | OpenAPI schema |

Application: [app/main.py](app/main.py). Tests: [tests/test_main.py](tests/test_main.py).

## Local development

Use Python 3.10 or newer (Python 3.12 recommended).

Windows PowerShell, from the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Linux/macOS:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000/docs>. Use `--reload` for development only.

## Deploy on your Ubuntu/Debian VM

These commands target a **fresh Ubuntu 24.04+ or Debian 12+ VM with systemd**.
Run them in Bash over SSH as your normal login user with sudo access.
They install system packages and replace the default Nginx welcome site. If the
VM already hosts websites or an API on port 8000, adapt the ports/site configuration
first; do not overwrite an existing deployment.

### 1. Install prerequisites and clone main

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git nginx curl
python3 --version

umask 022
sudo install -d -m 755 -o "$(id -un)" -g "$(id -gn)" /opt/hackathon-api
git clone --branch main --single-branch https://github.com/deepakmauryams/Hackathon-2026-Microsoft.git /opt/hackathon-api
cd /opt/hackathon-api
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
```

If the repository is private, authenticate Git on the VM first (for example, use
a read-only SSH deploy key and the SSH clone URL). Never put access tokens in the
clone URL, commands saved in shell history, or committed files. The clone command
expects an empty destination; use the update commands below for an existing clone.

### 2. Install the background API service

```bash
cd /opt/hackathon-api
sudo install -m 644 deploy/hackathon-api.service /etc/systemd/system/hackathon-api.service
sudo systemctl daemon-reload
sudo systemctl enable --now hackathon-api
sudo systemctl status hackathon-api --no-pager
curl --fail --retry 10 --retry-connrefused --retry-delay 1 http://127.0.0.1:8000/health
```

The [systemd service](deploy/hackathon-api.service) starts two Uvicorn workers,
runs as an unprivileged dynamic user with a read-only filesystem, restarts on
failure, and starts on boot. It does not depend on your SSH session. The application
and virtual environment must remain readable under `/opt/hackathon-api`; do not
move them into a home directory. This starter has no writable application storage.

### 3. Expose it through Nginx on port 80

```bash
cd /opt/hackathon-api
sudo install -m 644 deploy/nginx.conf /etc/nginx/sites-available/hackathon-api
sudo ln -sfn /etc/nginx/sites-available/hackathon-api /etc/nginx/sites-enabled/hackathon-api
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl enable --now nginx && sudo systemctl reload nginx
curl --fail http://127.0.0.1/health
```

The [Nginx configuration](deploy/nginx.conf) forwards requests to Uvicorn on
loopback only. **Do not open port 8000 to the Internet.**

### 4. Network access and HTTPS

- In your VM provider's network firewall/security-group settings, allow inbound
	**TCP 80** from your intended clients. Restrict SSH to your own source IP.
- If UFW is already enabled on the VM, allow HTTP with `sudo ufw allow 80/tcp`.
	Check with `sudo ufw status`. Do not enable UFW remotely without first allowing
	your actual SSH port, or you may lock yourself out.
- Open `http://YOUR_VM_PUBLIC_IP/docs` and `http://YOUR_VM_PUBLIC_IP/health`.
	The VM needs a reachable public IP or another route from your client.
- HTTP is suitable only for an initial non-sensitive demo. Before production or
	transmitting credentials, point a domain at the VM, replace `server_name _`
	with your domain, allow TCP 443, and configure a TLS certificate (for example,
	Let's Encrypt with Certbot). Then use HTTPS.

### Pull updates from main

Run as the same login user that originally cloned the repository:

```bash
cd /opt/hackathon-api
git pull --ff-only origin main && \
	.venv/bin/python -m pip install -r requirements.txt && \
	.venv/bin/python -m pip check && \
	sudo systemctl restart hackathon-api
sudo systemctl status hackathon-api --no-pager
curl --fail --retry 10 --retry-connrefused --retry-delay 1 http://127.0.0.1/health
```

This simple in-place deployment has a brief interruption; it is not a zero-downtime
rollout. If the tracked service or Nginx configuration changed, repeat steps 2 and
3 to install the updated configuration, then restart `hackathon-api` (enabling an
already-running service does not restart it).

### Troubleshooting

```bash
sudo journalctl -u hackathon-api -n 100 --no-pager
sudo systemctl status hackathon-api nginx --no-pager
sudo nginx -t
sudo tail -n 100 /var/log/nginx/error.log
sudo ss -ltnp
```

- **502 Bad Gateway**: check the API service logs and test port 8000 locally.
- **Works inside the VM, times out externally**: check the provider firewall,
	UFW, public IP, and routing.
- **Dependency installation fails**: verify Python is at least 3.10 and install
	packages using the virtual environment, not system-wide pip.
- **Git authentication fails**: configure access on the VM; local workstation
	Git credentials do not automatically transfer to it.

Keep secrets out of Git. The repository ignores local virtual environments,
Python caches, logs, and environment files.
