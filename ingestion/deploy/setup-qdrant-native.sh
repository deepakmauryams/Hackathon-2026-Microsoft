#!/usr/bin/env bash
# Qdrant ONLY. Run as your normal Ubuntu/Debian VM user, not via sudo bash.
set -Eeuo pipefail
umask 022
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
DATA=/data/files/cag/qdrant-native
QDRANT_VERSION=1.19.0

main() {
    [[ $# -eq 0 ]] || fail 'No arguments supported.'
    [[ $(uname -s) == Linux && $EUID -ne 0 ]] || fail 'Run as your normal Linux login user; the script uses sudo where needed.'
    for cmd in mountpoint readlink df awk sudo systemctl ss getent install sha256sum tar mktemp grep chmod; do
        command -v "$cmd" >/dev/null || fail "Missing prerequisite: $cmd"
    done
    mountpoint -q /data/files || fail '/data/files is not mounted. Nothing will be formatted or mounted by this script.'
    [[ $(readlink -f /data/files) == /data/files ]] || fail 'Refusing symlinked data mount.'
    local free arch asset checksum temp path os_id
    free=$(df -B1 --output=avail /data/files | awk 'NR==2 {print $1}')
    [[ $free =~ ^[0-9]+$ ]] && (( free > 5368709120 )) || fail 'Need more than 5 GiB free on /data/files.'
    case $(uname -m) in
        x86_64) arch=x86_64; checksum=9ec667456443463eee390e43cd36988af6b730c6db807b4e39f57c303d0264a3 ;;
        aarch64|arm64) arch=aarch64; checksum=8986afbbff9ac32d6e2dbe5cabec80565f613f777126096a461ba066573d3245 ;;
        *) fail 'Supported architectures: x86_64 and aarch64.' ;;
    esac
    sudo -v
    # Fresh installation only. Never adopt Docker storage or an existing service.
    for path in "$DATA" /opt/cag-qdrant /etc/cag-qdrant /etc/systemd/system/cag-qdrant.service; do
        if sudo test -e "$path" || sudo test -L "$path"; then
            fail "$path already exists. Inspect existing installation; use systemctl to manage it. No files overwritten."
        fi
    done
    for path in /data/files/cag /opt /etc/systemd/system; do
        [[ ! -L $path ]] || fail "Refusing symlink $path"
    done
    if systemctl cat cag-qdrant.service >/dev/null 2>&1; then fail 'cag-qdrant.service already exists.'; fi
    if [[ -n $(sudo ss -H -ltn 'sport = :6333') ]]; then fail 'Port 6333 is in use. Do not run Docker and native Qdrant together.'; fi
    if command -v docker >/dev/null && sudo docker info >/dev/null 2>&1; then
        if sudo docker ps --format '{{.Ports}}' | grep -Eq ':6333->'; then
            fail 'Docker publishes port 6333. Existing containers will not be stopped.'
        fi
    fi
    if getent passwd cag-qdrant >/dev/null || getent group cag-qdrant >/dev/null; then
        fail 'Account cag-qdrant already exists; review manually instead of adopting it.'
    fi
    # Isolate OS metadata: os-release also defines VERSION, NAME, etc.
    os_id=$(. /etc/os-release; printf '%s' "$ID")
    [[ $os_id == ubuntu || $os_id == debian ]] || fail 'This script supports Ubuntu/Debian.'
    sudo apt-get update
    sudo apt-get install -y --no-remove ca-certificates curl
    temp=$(mktemp -d)
    # Global variable so EXIT cleanup remains valid after main returns.
    NATIVE_TEMP=$temp
    trap 'rm -rf -- "$NATIVE_TEMP"' EXIT
    asset="qdrant-$arch-unknown-linux-musl.tar.gz"
    curl --disable --fail --show-error --location --proto '=https' --proto-redir '=https' \
        --retry 3 --connect-timeout 20 --max-time 600 \
        "https://github.com/qdrant/qdrant/releases/download/v$QDRANT_VERSION/$asset" -o "$temp/qdrant.tar.gz"
    printf '%s  %s\n' "$checksum" "$temp/qdrant.tar.gz" | sha256sum --check --status || fail 'Release checksum mismatch.'
    tar --extract --gzip --file "$temp/qdrant.tar.gz" --directory "$temp" --no-same-owner --no-same-permissions
    [[ -f $temp/qdrant && ! -L $temp/qdrant ]] || fail 'Expected qdrant binary missing.'
    chmod u+x "$temp/qdrant"
    "$temp/qdrant" --version
    # Recheck immediately before allocating persistent data.
    mountpoint -q /data/files || fail 'Data mount disappeared.'
    sudo useradd --system --user-group --no-create-home --home-dir "$DATA" --shell /usr/sbin/nologin cag-qdrant
    if [[ ! -e /data/files/cag ]]; then sudo install -d -m 0755 -o root -g root /data/files/cag; fi
    sudo install -d -m 0700 -o cag-qdrant -g cag-qdrant "$DATA"
    sudo install -d -m 0700 -o cag-qdrant -g cag-qdrant "$DATA/storage" "$DATA/snapshots" "$DATA/tmp"
    sudo install -d -m 0755 -o root -g root /opt/cag-qdrant /etc/cag-qdrant
    sudo install -m 0755 "$temp/qdrant" /opt/cag-qdrant/qdrant
    sudo install -m 0644 "$HERE/qdrant-native.yaml" /etc/cag-qdrant/config.yaml
    sudo install -m 0644 "$HERE/cag-qdrant.service" /etc/systemd/system/cag-qdrant.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now cag-qdrant.service
    if ! curl --disable --fail --silent --show-error --noproxy '*' --connect-timeout 3 --max-time 5 --retry 15 --retry-connrefused \
        --retry-delay 2 --retry-max-time 45 http://127.0.0.1:6333/readyz; then
        sudo journalctl -u cag-qdrant.service -n 40 --no-pager
        fail 'Qdrant readiness failed. Installed files retained for diagnosis; do not blindly rerun setup.'
    fi
    printf '\nQdrant is ready on 127.0.0.1:6333. No scraper or collection was started.\n'
    curl --disable --fail --silent --show-error --noproxy '*' http://127.0.0.1:6333/collections
}
main "$@"