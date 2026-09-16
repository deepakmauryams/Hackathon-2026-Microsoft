#!/usr/bin/env bash
# Run as the normal VM login user: bash deploy/setup.sh
set -Eeuo pipefail

APP_DIR=/opt/hackathon-api
REPO_URL=https://github.com/deepakmauryams/Hackathon-2026-Microsoft.git

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
step() { printf '\n==> %s\n' "$*"; }

on_error() {
    printf '\nSetup failed at line %s. Fix the error above and rerun the script.\n' "$1" >&2
    printf 'Service logs: sudo journalctl -u hackathon-api -n 100 --no-pager\n' >&2
}
trap 'on_error "$LINENO"' ERR

main() {
    [[ $# -eq 0 ]] || fail 'No arguments supported. Run: bash deploy/setup.sh'
    [[ $(uname -s) == Linux ]] || fail 'Run this script on the Ubuntu/Debian VM, not Windows.'
    [[ $EUID -ne 0 ]] || fail 'Run as your normal login user without sudo; the script uses sudo where needed.'
    command -v apt-get >/dev/null || fail 'Ubuntu/Debian with apt-get is required.'
    [[ -d /run/systemd/system ]] || fail 'A running systemd installation is required.'
    command -v sudo >/dev/null || fail 'Your login user needs sudo access.'
    sudo -v
    umask 022

    step 'Install system prerequisites'
    sudo apt-get update
    sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip git nginx curl
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "Python 3.10+ is required")'

    step 'Clone or update main (local changes are never discarded)'
    if [[ ! -d "$APP_DIR" ]]; then
        sudo install -d -m 755 -o "$(id -un)" -g "$(id -gn)" "$APP_DIR"
    fi
    [[ -w "$APP_DIR" ]] || fail "$APP_DIR must be writable by your login user. Use the original deployment user."
    if [[ ! -d "$APP_DIR/.git" ]]; then
        [[ -z $(ls -A "$APP_DIR") ]] || fail "$APP_DIR is not an empty directory or a Git clone."
        git clone --branch main --single-branch "$REPO_URL" "$APP_DIR"
    fi
    cd "$APP_DIR"
    case "$(git remote get-url origin)" in
        "$REPO_URL"|https://github.com/deepakmauryams/Hackathon-2026-Microsoft|git@github.com:deepakmauryams/Hackathon-2026-Microsoft.git) ;;
        *) fail 'Unexpected Git origin. Refusing to deploy a different repository.' ;;
    esac
    [[ $(git branch --show-current) == main ]] || fail 'The deployment clone must be on main.'
    [[ -z $(git status --porcelain) ]] || fail 'Deployment clone has local changes. Commit or move them before rerunning.'
    git fetch origin main
    git merge-base --is-ancestor HEAD origin/main || fail 'Local main has unpushed or divergent commits. Resolve them first.'
    git merge --ff-only origin/main

    step 'Check Nginx site conflicts'
    local site
    for site in /etc/nginx/sites-enabled/*; do
        [[ -e "$site" || -L "$site" ]] || continue
        case "$(basename "$site")" in
            default|hackathon-api) ;;
            *) fail "Another Nginx site is enabled: $site. Configure shared hosting manually." ;;
        esac
    done
    if [[ -e /etc/nginx/sites-enabled/default || -L /etc/nginx/sites-enabled/default ]]; then
        [[ $(readlink -f /etc/nginx/sites-enabled/default) == /etc/nginx/sites-available/default ]] ||
            fail 'The default Nginx site is customized. Configure shared hosting manually.'
    fi

    step 'Install Python dependencies'
    if [[ ! -x .venv/bin/python ]]; then
        python3 -m venv .venv
    fi
    .venv/bin/python -m pip install -r requirements.txt
    .venv/bin/python -m pip check
    .venv/bin/python -c 'from app.main import app; print("Application import OK:", app.title)'

    step 'Install and restart the API service'
    sudo install -m 644 deploy/hackathon-api.service /etc/systemd/system/hackathon-api.service
    sudo systemctl daemon-reload
    sudo systemctl enable hackathon-api
    sudo systemctl restart hackathon-api
    curl --fail --silent --show-error --max-time 5 --retry 10 --retry-connrefused --retry-delay 1 http://127.0.0.1:8000/health

    step 'Configure Nginx (preserve any existing API site, including HTTPS)'
    if [[ ! -e /etc/nginx/sites-available/hackathon-api ]]; then
        sudo install -m 644 deploy/nginx.conf /etc/nginx/sites-available/hackathon-api
    fi
    local added_site=false
    if [[ -e /etc/nginx/sites-enabled/hackathon-api || -L /etc/nginx/sites-enabled/hackathon-api ]]; then
        [[ $(readlink -f /etc/nginx/sites-enabled/hackathon-api) == /etc/nginx/sites-available/hackathon-api ]] ||
            fail 'The enabled API site points to an unexpected location. Review it manually.'
    else
        sudo ln -s /etc/nginx/sites-available/hackathon-api /etc/nginx/sites-enabled/hackathon-api
        added_site=true
    fi
    # Only disable the default site after saving its symlink target for rollback.
    local default_target=''
    if [[ -L /etc/nginx/sites-enabled/default ]]; then
        default_target=$(readlink /etc/nginx/sites-enabled/default)
        sudo rm /etc/nginx/sites-enabled/default
    fi
    if ! sudo nginx -t; then
        if [[ "$added_site" == true ]]; then
            sudo rm /etc/nginx/sites-enabled/hackathon-api
        fi
        if [[ -n "$default_target" ]]; then
            sudo ln -s "$default_target" /etc/nginx/sites-enabled/default
        fi
        fail 'Nginx validation failed; Nginx was not reloaded. Check the enabled site configuration.'
    fi
    sudo systemctl enable --now nginx
    sudo systemctl reload nginx
    # Reload signals Nginx asynchronously: old workers can briefly return 404.
    # --retry-all-errors includes 404, unlike curl's default retry policy.
    if ! curl --fail --silent --show-error --noproxy '*' --max-time 5 \
        --retry 10 --retry-all-errors --retry-delay 1 --retry-max-time 30 \
        http://127.0.0.1/health; then
        printf '\nNginx health check still fails after retries. Check routing:\n' >&2
        printf '  sudo nginx -T\n' >&2
        printf '  sudo journalctl -u nginx -n 50 --no-pager\n' >&2
        printf '  curl -i http://127.0.0.1:8000/health\n' >&2
        fail 'API startup passed, but the Nginx route is not healthy. Existing site settings were preserved.'
    fi

    printf '\n\nSetup complete. API: http://YOUR_VM_PUBLIC_IP/docs\n'
    printf 'Allow inbound TCP 80 in your VM network firewall (and UFW if active).\n'
    printf 'Port 8000 stays private. Configure HTTPS before sending sensitive data.\n'
    printf 'Next update: cd /opt/hackathon-api && git pull --ff-only origin main && bash deploy/setup.sh\n'
}

# Define the entire function before running it: pulling Git may update this file.
main "$@"