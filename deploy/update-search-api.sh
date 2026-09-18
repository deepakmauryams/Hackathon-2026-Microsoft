#!/usr/bin/env bash
# USER-RUN ONLY. No changes to the base API, worker, Qdrant, nginx, or env files.
set -Eeuo pipefail
umask 022
BASE=/opt/cag-search-api
DROP_DIR=/etc/systemd/system/hackathon-api.service.d
DROPIN=$DROP_DIR/cag-search.conf
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
SOURCE=$(cd -- "$HERE/.." && pwd -P)
RELEASE=''
ARMED=0
LOCKED=0

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

trusted_path() {
    local mode
    # api.env/public.env live below a root-only (0700) directory. Inspect only
    # metadata as root; never read credentials or weaken directory permissions.
    sudo test -e "$1" && ! sudo test -L "$1" &&
        [[ $(sudo readlink -f -- "$1") == "$1" ]] || fail "Missing/symlinked path: $1"
    [[ $(sudo stat -c %u -- "$1") == 0 ]] || fail "Not root-owned: $1"
    mode=$(sudo stat -c %a -- "$1")
    (( (8#$mode & 0022) == 0 )) || fail "Group/world writable path: $1"
}

ensure_dir() {
    [[ ! -L $1 ]] || fail "Refusing symlink: $1"
    if [[ ! -e $1 ]]; then sudo install -d -o root -g root -m 0755 -- "$1"; fi
    trusted_path "$1"
    [[ -d $1 ]] || fail "Not a directory: $1"
}

sealed_release() {
    [[ $1 =~ ^/opt/cag-search-api/releases/[0-9]{8}T[0-9]{6}Z\.[A-Za-z0-9]{8}$ ]] || fail 'Unknown release path.'
    trusted_path "$1"
    trusted_path "$1/.sealed"
    [[ -f $1/.sealed && $(cat -- "$1/.sealed") == cag-search-api-v1 ]] || fail 'Unknown release marker.'
    trusted_path "$1/cag-search.conf"
}

known_dropin() {
    local candidate
    [[ ! -L $DROPIN ]] || fail 'Refusing symlinked drop-in.'
    [[ -e $DROPIN ]] || return 0
    trusted_path "$DROPIN"
    [[ -f $DROPIN ]] || fail 'Drop-in must be a regular file.'
    for candidate in "$BASE"/releases/*/cag-search.conf; do
        [[ -f $candidate && ! -L $candidate && -f ${candidate%/*}/.sealed ]] || continue
        if cmp -s -- "$candidate" "$DROPIN"; then
            sealed_release "${candidate%/*}"
            return 0
        fi
    done
    fail 'Unknown/modified cag-search.conf: preserve it and resolve manually; it will not be overwritten.'
}

atomic_dropin() {
    local temporary
    [[ ! -L $DROPIN ]] || return 1
    temporary=$(sudo mktemp "$DROP_DIR/.cag-search.XXXXXXXX") || return 1
    sudo install -o root -g root -m 0644 -- "$1" "$temporary" || return 1
    sudo mv -T -- "$temporary" "$DROPIN"
}

restore_previous() {
    # Do not remove/overwrite a file installed by someone else during this run.
    [[ ! -L $DROPIN ]] || return 1
    if [[ -e $DROPIN ]]; then
        cmp -s -- "$DROPIN" "$RELEASE/cag-search.conf" ||
            { [[ -f $RELEASE/previous.conf ]] && cmp -s -- "$DROPIN" "$RELEASE/previous.conf"; } || return 1
    fi
    if [[ -f $RELEASE/previous.conf && ! -L $RELEASE/previous.conf && ! -e $RELEASE/previous.absent ]]; then
        trusted_path "$RELEASE/previous.conf"
        atomic_dropin "$RELEASE/previous.conf" || return 1
    elif [[ -f $RELEASE/previous.absent && ! -L $RELEASE/previous.absent && ! -e $RELEASE/previous.conf ]]; then
        trusted_path "$RELEASE/previous.absent"
        if [[ -e $DROPIN ]]; then sudo rm -- "$DROPIN" || return 1; fi
    else
        return 1
    fi
    sudo systemctl daemon-reload || return 1
    sudo systemctl restart hackathon-api.service
}

cleanup() {
    local status=$?
    trap - EXIT
    if (( ARMED )); then
        printf 'Activation failed/interrupted; restoring the previous drop-in and restarting the old API.\n' >&2
        if restore_previous; then
            printf 'Previous API configuration restored.\n' >&2
        else
            printf 'ROLLBACK FAILED: retain this release and inspect services manually; do not delete state.\n' >&2
        fi
        status=1
    fi
    if (( LOCKED )); then sudo rmdir -- "$BASE/.update-lock" || status=1; fi
    if (( status != 0 )) && [[ -n $RELEASE ]]; then
        printf 'Failed release retained: %s (retry creates a new release).\n' "$RELEASE" >&2
    fi
    exit "$status"
}

health_check() {
    local attempt
    for attempt in {1..15}; do
        if systemctl is-active --quiet hackathon-api.service &&
            curl --disable --fail --silent --noproxy '*' --connect-timeout 2 --max-time 3 \
                --output /dev/null http://127.0.0.1:8000/health; then return 0; fi
        sleep 2
    done
    return 1
}

main() {
    [[ $EUID -ne 0 && $(id -un) == azureuser && $(uname -s) == Linux ]] || fail 'Run as the normal azureuser login, NOT sudo bash.'
    [[ $# -eq 0 || ( $# -eq 2 && $1 == --rollback ) ]] || fail 'Usage: bash update-search-api.sh [--rollback /opt/cag-search-api/releases/ID]'
    local cmd unit file path free tag check os_id os_version
    for cmd in sudo systemctl systemd-run mountpoint readlink stat df awk install curl cmp mktemp python3 find; do
        command -v "$cmd" >/dev/null || fail "Missing prerequisite: $cmd (no packages are installed automatically)."
    done
    os_id=$(. /etc/os-release; printf '%s' "$ID")
    os_version=$(. /etc/os-release; printf '%s' "$VERSION_ID")
    [[ $os_id == ubuntu && $os_version == 24.04 ]] || fail 'This installer targets Ubuntu 24.04/systemd.'
    sudo -v
    for path in /opt /etc /etc/systemd /etc/systemd/system; do trusted_path "$path"; done
    if [[ ! -e $BASE && ! -L $BASE ]]; then
        [[ $# -eq 0 ]] || fail 'No deployment to roll back.'
        sudo mkdir -m 0755 -- "$BASE"
        printf 'cag-search-api-v1\n' | sudo tee "$BASE/.managed" >/dev/null
        sudo chmod 0644 "$BASE/.managed"
    fi
    trusted_path "$BASE"
    trusted_path "$BASE/.managed"
    [[ -f $BASE/.managed && $(cat "$BASE/.managed") == cag-search-api-v1 ]] || fail 'Unknown deployment directory.'
    ensure_dir "$BASE/releases"
    ensure_dir "$DROP_DIR"
    # Other persistent drop-ins may override safety/runtime settings. Require review.
    for file in "$DROP_DIR"/*; do
        [[ ! -e $file && ! -L $file ]] && continue
        [[ $file == "$DROPIN" ]] || fail "Unknown file in drop-in directory: $file"
    done
    sudo mkdir -- "$BASE/.update-lock" || fail 'Deployment lock exists; check for a running installer before manual recovery.'
    LOCKED=1
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    known_dropin
    if [[ $# -eq 2 ]]; then
        RELEASE=$2
        sealed_release "$RELEASE"
        [[ -f $DROPIN ]] && cmp -s -- "$DROPIN" "$RELEASE/cag-search.conf" || fail 'Rollback target is not the currently active deployment.'
        restore_previous || fail 'Could not restore/restart the previous API; inspect manually.'
        health_check || fail 'Previous configuration restored, but its health check failed.'
        printf 'Rolled back deployment %s; all releases and data retained.\n' "$RELEASE"
        return
    fi
    mountpoint -q /data/files || fail '/data/files must already be mounted; nothing will be mounted/formatted.'
    # This is existing data storage, not a trusted executable/config directory.
    # Its owner is chosen by the volume operator; never chown/chmod the mount.
    sudo test -d /data/files && ! sudo test -L /data/files &&
        [[ $(sudo readlink -f -- /data/files) == /data/files ]] || fail 'Data mount must be a real, non-symlinked directory.'
    for path in /data /opt/cag-scraper /opt/cag-scraper/app /opt/cag-scraper/venv /etc/cag-scraper /etc/hackathon-api; do
        trusted_path "$path"
    done
    for file in /etc/cag-scraper/worker.env /etc/hackathon-api/api.env /etc/hackathon-api/public.env; do
        # Check path metadata only. Never source/read/print the API secret files.
        trusted_path "$file"
        sudo test -f "$file" || fail "Required environment file missing: $file"
    done
    for unit in hackathon-api cag-qdrant cag-scraper; do
        systemctl is-active --quiet "$unit.service" || fail "Existing service must be active: $unit"
    done
    local dropins
    dropins=$(systemctl show hackathon-api.service -p DropInPaths --value)
    [[ -z $dropins || $dropins == "$DROPIN" ]] || fail 'Unknown effective service drop-ins; review them before deployment.'
    [[ $(systemctl show hackathon-api.service -p DynamicUser --value) == yes ]] || fail 'Expected existing DynamicUser API service.'
    local env_files
    env_files=$(systemctl show hackathon-api.service -p EnvironmentFiles --value)
    [[ $env_files == *'/etc/hackathon-api/api.env '* && $env_files == *'/etc/hackathon-api/public.env '* ]] || fail 'Expected inherited API environment files.'
    free=$(df -B1 --output=avail /opt | awk 'NR==2 {print $1}')
    [[ $free =~ ^[0-9]+$ ]] && (( free >= 4294967296 )) || fail 'At least 4 GiB free on /opt is required.'
    for path in "$SOURCE/app" "$SOURCE/ingestion" "$HERE"; do
        [[ -d $path && $(readlink -f -- "$path") == "$path" ]] || fail 'Symlinked/missing source directory.'
    done
    # Expand only the app source glob from the trusted bundle root, not the login cwd.
    cd "$SOURCE"
    local sources=(app/*.py ingestion/__init__.py ingestion/catalogue_encoder.py requirements.txt
        deploy/package-search-api.py deploy/update-search-api.sh deploy/snapshot-search-model.py
        deploy/verify-search-api.py deploy/QDRANT-API.md)
    for file in "${sources[@]}"; do
        [[ -f $file && ! -L $file ]] || fail "Missing/symlinked source: $file"
    done
    [[ -f app/main.py && -f app/retrieval.py && -f app/__init__.py ]] || fail 'Incomplete API bundle.'
    RELEASE=$(sudo mktemp -d "$BASE/releases/$(date -u +%Y%m%dT%H%M%SZ).XXXXXXXX")
    tag=${RELEASE##*/}
    sudo chmod 0755 "$RELEASE"
    for file in "${sources[@]}"; do sudo install -D -o root -g root -m 0644 -- "$file" "$RELEASE/$file"; done
    sudo install -d -m 0700 -o cag-scraper -g cag-scraper "$RELEASE/snapshot"
    # Accessible cwd BEFORE identity change; only this explicit stage is writable.
    cd /opt/cag-scraper/app
    sudo systemd-run --quiet --wait --pipe --collect --unit="cag-search-snapshot-$tag" \
        -p User=cag-scraper -p Group=cag-scraper -p WorkingDirectory=/opt/cag-scraper/app \
        -p ProtectSystem=strict -p ProtectHome=yes -p PrivateTmp=yes -p NoNewPrivileges=yes \
        -p "ReadWritePaths=$RELEASE/snapshot" -p RestrictAddressFamilies=AF_UNIX \
        -p IPAddressDeny=any -p MemoryMax=2G -p CPUQuota=100% -p RuntimeMaxSec=600 \
        /bin/bash -c 'set -e; set -a; . /etc/cag-scraper/worker.env; set +a; export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 CAG_THREADS=1 PYTHONDONTWRITEBYTECODE=1 TMPDIR=/tmp; exec /opt/cag-scraper/venv/bin/python "$1" "$2"' \
        snapshot "$RELEASE/deploy/snapshot-search-model.py" "$RELEASE/snapshot"
    [[ -z $(sudo find "$RELEASE/snapshot" -type l -print -quit) ]] || fail 'Snapshot must contain bytes, not symlinks.'
    sudo chown -h -R -P root:root "$RELEASE/snapshot"
    sudo chmod -R u=rwX,go=rX "$RELEASE/snapshot"
    sudo install -d -m 0755 -o azureuser -g "$(id -gn)" "$RELEASE/.venv" "$RELEASE/build-home"
    cd "$RELEASE"
    /opt/cag-scraper/venv/bin/python -m venv --copies "$RELEASE/.venv"
    # Install only into the new login-user-owned venv. No worker pip/cache writes.
    env -i HOME="$RELEASE/build-home" PATH=/usr/bin:/bin PIP_CONFIG_FILE=/dev/null \
        "$RELEASE/.venv/bin/python" -m pip --disable-pip-version-check install --no-cache-dir \
        --only-binary=:all: --index-url=https://pypi.org/simple \
        -r "$RELEASE/requirements.txt" -c "$RELEASE/snapshot/worker-constraints.txt"
    "$RELEASE/.venv/bin/python" -m pip check
    sudo chown -h -R -P root:root "$RELEASE"
    sudo chmod -R u=rwX,go=rX "$RELEASE"
    check="cag-search-check-$tag"
    sudo systemd-run --quiet --wait --pipe --collect --unit="$check" \
        -p DynamicUser=yes -p "WorkingDirectory=$RELEASE" -p "RuntimeDirectory=$check" \
        -p ProtectSystem=strict -p ProtectHome=yes -p PrivateTmp=yes -p NoNewPrivileges=yes \
        -p IPAddressDeny=any -p IPAddressAllow=localhost -p MemoryMax=2G -p CPUQuota=100% -p RuntimeMaxSec=300 \
        /usr/bin/env -i PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
        "CAG_MODEL_PATH=$RELEASE/snapshot/model" "CAG_MODEL_CACHE=/run/$check/models" \
        CAG_THREADS=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false \
        HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 \
        NO_PROXY=127.0.0.1,localhost,::1 no_proxy=127.0.0.1,localhost,::1 \
        QDRANT_URL=http://127.0.0.1:6333 CAG_COLLECTION=cag_catalogue_v1 \
        "$RELEASE/.venv/bin/python" "$RELEASE/deploy/verify-search-api.py"
    # Published, profile-compatible Qdrant points + a valid passage prove scraper progress.
    # No service configuration has been switched before this gate succeeds.
    cat <<EOF | sudo tee "$RELEASE/cag-search.conf" >/dev/null
# Managed by cag-search-api v1; release $RELEASE
[Unit]
After=cag-qdrant.service
Requires=cag-qdrant.service
RequiresMountsFor=/data/files

[Service]
DynamicUser=yes
ProtectSystem=strict
ProtectHome=yes
NoNewPrivileges=yes
WorkingDirectory=$RELEASE
ExecStart=
ExecStart=$RELEASE/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips=127.0.0.1
ExecStartPre=$RELEASE/.venv/bin/python $RELEASE/deploy/verify-search-api.py
TimeoutStartSec=180
MemoryMax=2G
CPUQuota=100%
StateDirectory=hackathon-api
StateDirectoryMode=0700
Environment=CAG_MODEL_PATH=$RELEASE/snapshot/model
Environment=CAG_MODEL_CACHE=/var/lib/hackathon-api/models
Environment=HF_HOME=/var/lib/hackathon-api/hf
Environment=HOME=/var/lib/hackathon-api
Environment=XDG_CACHE_HOME=/var/lib/hackathon-api/cache
Environment=CAG_THREADS=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false
Environment=HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
Environment=NO_PROXY=127.0.0.1,localhost,::1 no_proxy=127.0.0.1,localhost,::1
Environment=QDRANT_URL=http://127.0.0.1:6333 CAG_COLLECTION=cag_catalogue_v1
UnsetEnvironment=HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy QDRANT_API_KEY
IPAddressDeny=any
IPAddressAllow=localhost
InaccessiblePaths=/data/files/cag/worker-native /opt/cag-scraper
# api.env/public.env, PrivateTmp and other base-unit hardening remain inherited.
EOF
    known_dropin
    if [[ -e $DROPIN ]]; then
        sudo install -o root -g root -m 0644 "$DROPIN" "$RELEASE/previous.conf"
    else
        sudo touch "$RELEASE/previous.absent"
    fi
    printf 'cag-search-api-v1\n' | sudo tee "$RELEASE/.sealed" >/dev/null
    sudo chmod 0644 "$RELEASE/.sealed" "$RELEASE/cag-search.conf"
    mountpoint -q /data/files || fail 'Data mount disappeared before activation.'
    ARMED=1
    atomic_dropin "$RELEASE/cag-search.conf"
    sudo systemctl daemon-reload
    sudo systemctl restart hackathon-api.service
    [[ $(systemctl show hackathon-api.service -p WorkingDirectory --value) == "$RELEASE" ]] || fail 'Effective service directory was overridden.'
    [[ $(systemctl show hackathon-api.service -p DynamicUser --value) == yes ]] || fail 'Effective service lost DynamicUser.'
    health_check || fail 'Activated API did not become healthy.'
    ARMED=0
    printf 'Activated %s\nRollback this deployment:\n  bash %s/deploy/update-search-api.sh --rollback %s\n' "$RELEASE" "$RELEASE" "$RELEASE"
}

main "$@"