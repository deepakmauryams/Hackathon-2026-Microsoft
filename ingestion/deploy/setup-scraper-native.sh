#!/usr/bin/env bash
# Fresh native worker install. Does not install/reconfigure Qdrant or start crawling.
set -Eeuo pipefail
umask 022
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
SOURCE=$(cd -- "$HERE/.." && pwd -P)
APP=/opt/cag-scraper
DATA=/data/files/cag/worker-native

main() {
    [[ $# -eq 0 ]] || fail 'No arguments supported.'
    [[ $(uname -s) == Linux && $EUID -ne 0 ]] || fail 'Run as your normal Linux login user, not sudo bash.'
    for cmd in sudo mountpoint readlink df awk systemctl getent install curl; do
        command -v "$cmd" >/dev/null || fail "Missing prerequisite: $cmd"
    done
    local os_id free path file
    os_id=$(. /etc/os-release; printf '%s' "$ID")
    [[ $os_id == ubuntu || $os_id == debian ]] || fail 'Ubuntu/Debian with systemd is required.'
    mountpoint -q /data/files || fail '/data/files must already be mounted. Nothing will be mounted or formatted.'
    [[ $(readlink -f /data/files) == /data/files ]] || fail 'Refusing symlinked mount.'
    free=$(df -B1 --output=avail /data/files | awk 'NR==2 {print $1}')
    # Model download + temporary PDF work need headroom beyond the runtime reserve.
    [[ $free =~ ^[0-9]+$ ]] && (( free > 8589934592 )) || fail 'Need more than 8 GiB free on /data/files for installation.'
    free=$(df -B1 --output=avail /opt | awk 'NR==2 {print $1}')
    [[ $free =~ ^[0-9]+$ ]] && (( free > 3221225472 )) || fail 'Need more than 3 GiB free on the /opt filesystem for the Python environment.'
    [[ -f $SOURCE/catalogue.py && -f $SOURCE/requirements.txt ]] || fail 'Copy/extract the whole ingestion package, not just this script.'
    for file in cag-scraper.env cag-scraper.service cag-scraper.sh native-preflight.py; do
        [[ -f $HERE/$file && ! -L $HERE/$file ]] || fail "Missing regular setup file: $file"
    done
    sudo -v
    for path in "$APP" "$DATA" /etc/cag-scraper /usr/local/bin/cag-scraper /etc/systemd/system/cag-scraper.service; do
        if sudo test -e "$path" || sudo test -L "$path"; then
            fail "$path exists; this is a fresh-install script. Preserve data and inspect the existing install, do not blindly rerun."
        fi
    done
    for path in /data/files/cag /opt /etc/systemd/system /usr/local/bin; do
        [[ ! -L $path ]] || fail "Refusing symlink $path"
    done
    if getent passwd cag-scraper >/dev/null || getent group cag-scraper >/dev/null; then
        fail 'cag-scraper account/group exists; inspect manually instead of adopting it.'
    fi
    if systemctl cat cag-scraper.service >/dev/null 2>&1; then fail 'Scraper service already exists.'; fi
    systemctl is-active --quiet cag-qdrant.service || fail 'Start the existing cag-qdrant service first.'
    curl --disable --fail --silent --show-error --noproxy '*' --connect-timeout 3 --max-time 10 http://127.0.0.1:6333/readyz
    printf '\nInstalling Python and English/Hindi OCR dependencies...\n'
    sudo apt-get update
    sudo apt-get install -y --no-remove python3 python3-venv ca-certificates poppler-utils \
        tesseract-ocr tesseract-ocr-eng tesseract-ocr-hin
    python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
    mountpoint -q /data/files || fail 'Data mount disappeared.'
    # From here failures retain files for diagnosis. No automatic deletion or adoption.
    trap 'printf "Setup failed. Partial installation retained; do not delete state or rerun blindly. Share the error for recovery.\n" >&2' ERR
    sudo useradd --system --user-group --no-create-home --home-dir "$DATA" --shell /usr/sbin/nologin cag-scraper
    if [[ ! -e /data/files/cag ]]; then sudo install -d -m 0755 -o root -g root /data/files/cag; fi
    sudo install -d -m 0700 -o cag-scraper -g cag-scraper "$DATA" "$DATA/pipeline" "$DATA/models" "$DATA/tmp"
    sudo install -d -m 0755 -o root -g root "$APP" "$APP/app/ingestion" "$APP/app/ingestion/deploy" /etc/cag-scraper
    for file in "$SOURCE"/*.py; do
        [[ ! -L $file ]] || fail "Refusing symlink source: $file"
        sudo install -m 0644 "$file" "$APP/app/ingestion/$(basename "$file")"
    done
    sudo install -m 0644 "$SOURCE/requirements.txt" "$APP/app/ingestion/requirements.txt"
    sudo install -m 0644 "$HERE/native-preflight.py" "$APP/app/ingestion/deploy/native-preflight.py"
    sudo install -m 0644 "$HERE/cag-scraper.env" /etc/cag-scraper/worker.env
    sudo install -m 0755 "$HERE/cag-scraper.sh" /usr/local/bin/cag-scraper
    # sudo preserves cwd; the worker cannot traverse azureuser's private home.
    cd "$APP/app"
    # Install Python packages as the restricted worker, never pip as root.
    sudo install -d -m 0755 -o cag-scraper -g cag-scraper "$APP/venv"
    sudo -u cag-scraper python3 -m venv "$APP/venv"
    sudo -u cag-scraper env HOME="$DATA/models" TMPDIR="$DATA/tmp" \
        "$APP/venv/bin/python" -m pip install --no-cache-dir --disable-pip-version-check \
        -r "$APP/app/ingestion/requirements.txt"
    sudo -u cag-scraper "$APP/venv/bin/python" -m pip check
    sudo chown -R root:root "$APP/venv"
    /usr/local/bin/cag-scraper check
    sudo install -m 0644 "$HERE/cag-scraper.service" /etc/systemd/system/cag-scraper.service
    sudo systemctl daemon-reload
    printf '\nNative scraper installed. Service is NOT started or enabled yet.\n'
    printf 'Validate one cycle (downloads model and processes one report):\n  cag-scraper run --once\n'
    printf 'Inspect coverage:\n  cag-scraper status\n'
    printf 'Then start continuous background ingestion:\n  sudo systemctl enable --now cag-scraper\n'
}
main "$@"