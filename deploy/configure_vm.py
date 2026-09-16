"""Root-only VM configuration; never print or commit generated credentials."""

import os
from pathlib import Path
import re
import secrets
import shutil

LIMITS_INCLUDE = "include /etc/nginx/snippets/hackathon-api-limits.conf;"


def add_proxy_limits(config: str) -> str:
    """Insert into each root proxy location without replacing domain/TLS settings."""
    location = re.compile(r"(location\s+/\s*\{)([^{}]*)", re.MULTILINE)
    matches = list(location.finditer(config))
    if not matches:
        raise ValueError("No simple 'location /' block found; add the limits include manually")

    def replace(match: re.Match) -> str:
        body = match.group(2)
        if LIMITS_INCLUDE in body:
            return match.group(0)
        if re.search(r"\b(client_max_body_size|limit_req|limit_req_status)\b", body):
            raise ValueError("Existing location-level limits found; merge limits manually")
        return match.group(1) + "\n        " + LIMITS_INCLUDE + body

    return location.sub(replace, config)


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("Run through setup.sh, which requests sudo for this step")
    directory = Path("/etc/hackathon-api")
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    secret = directory / "api.env"
    if not secret.exists():
        fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(f"RAG_API_KEY={secrets.token_urlsafe(48)}\n")
    else:
        if secret.is_symlink():
            raise SystemExit("Refusing a symlinked API key file")
        secret.chmod(0o600)

    site = Path("/etc/nginx/sites-available/hackathon-api")
    original = site.read_text()
    updated = add_proxy_limits(original)
    if original != updated:
        shutil.copy2(site, site.with_name("hackathon-api.before-limits"))
        site.write_text(updated)


if __name__ == "__main__":
    main()