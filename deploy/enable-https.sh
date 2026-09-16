#!/usr/bin/env bash
# Run AFTER setup.sh, with a DNS hostname pointing at this VM.
set -Eeuo pipefail

main() {
    [[ $# -eq 2 ]] || { echo 'Usage: bash deploy/enable-https.sh api.example.com you@example.com' >&2; exit 1; }
    [[ $EUID -ne 0 ]] || { echo 'Run as your normal login user, not root.' >&2; exit 1; }
    local domain=${1,,} email=$2
    python3 - "$domain" "$email" <<'PY'
import re
import sys
domain, email = sys.argv[1:]
labels = domain.split('.')
if (len(domain) > 253 or len(labels) < 2 or not re.fullmatch(r'[a-z]{2,63}', labels[-1])
        or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)):
    sys.exit('Provide a valid DNS hostname, not an IP address or URL.')
if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
    sys.exit('Provide a valid certificate contact email.')
PY
    getent ahosts "$domain" >/dev/null || { echo 'Create a DNS A record pointing to this VM first.' >&2; exit 1; }
    sudo -v
    sudo test -f /etc/hackathon-api/api.env || { echo 'Run setup.sh first.' >&2; exit 1; }
    echo "Requesting a certificate for $domain; TCP 80 and 443 must be reachable."
    echo 'This command accepts the Let’s Encrypt subscriber agreement on your behalf.'
    sudo apt-get update
    sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y certbot python3-certbot-nginx
    sudo python3 - "$domain" <<'PY'
from pathlib import Path
import re
import shutil
import sys
path = Path('/etc/nginx/sites-available/hackathon-api')
text = path.read_text()
domain = sys.argv[1]
updated, count = re.subn(r'server_name\s+_\s*;', f'server_name {domain};', text)
if not count and not re.search(r'server_name\s+' + re.escape(domain) + r'\s*;', text):
    sys.exit('Existing Nginx hostname differs. Review the site manually; nothing overwritten.')
if updated != text:
    shutil.copy2(path, path.with_name('hackathon-api.before-https'))
    path.write_text(updated)
PY
    sudo nginx -t
    sudo systemctl reload nginx
    sudo certbot --nginx --non-interactive --agree-tos --email "$email" --domain "$domain" --redirect
    # Non-secret origin used in the OpenAPI servers list, never inferred from Host.
    printf 'PUBLIC_BASE_URL=https://%s\n' "$domain" | sudo tee /etc/hackathon-api/public.env >/dev/null
    sudo chmod 600 /etc/hackathon-api/public.env
    sudo systemctl restart hackathon-api
    sudo systemctl enable --now certbot.timer
    curl --fail --silent --show-error --resolve "$domain:443:127.0.0.1" \
        --noproxy '*' --max-time 5 --retry 10 --retry-all-errors --retry-delay 1 \
        --retry-max-time 30 "https://$domain/health"
    echo
    echo "HTTPS ready: https://$domain/docs"
    echo "Foundry OpenAPI URL: https://$domain/openapi.json"
    echo 'Keep TCP 80 available for ACME renewal; HTTP redirects to HTTPS.'
    echo 'If ONLY 443 may be open, use automated DNS-01 validation instead of this HTTP-01 setup.'
    echo 'Test renewal separately: sudo certbot renew --dry-run'
}

main "$@"