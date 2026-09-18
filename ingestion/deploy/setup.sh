#!/usr/bin/env bash
# Run from the checked-out repository as the normal Linux VM login user.
set -Eeuo pipefail
umask 022

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
step() { printf '\n==> %s\n' "$*"; }
trap 'printf "Setup failed at line %s; inspect the error and rerun. No unrelated containers are stopped.\n" "$LINENO" >&2' ERR

DEPLOY_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT=$(cd -- "$DEPLOY_DIR/../.." && pwd -P)
ROOT=/data/files/cag
PROJECT=cag-catalogue
RESERVE_BYTES=5368709120
DOCKER=()
COMPOSE=(docker compose --project-name "$PROJECT" -f "$DEPLOY_DIR/compose.yaml")

# Keep Compose and all inspections on the same, verified local engine.
docker() { "${DOCKER[@]}" "$@"; }

check_disk() {
    # No package operation, mkdir, Docker start, image pull or volume operation before these checks.
    mountpoint -q /data/files || fail '/data/files must already be a mounted filesystem. Ask the VM administrator to mount the data volume; this script never mounts or formats disks.'
    [[ $(readlink -f /data/files) == /data/files ]] || fail '/data/files (including its parents) must not be a symlink.'
    local available
    available=$(df -B1 --output=avail /data/files | tail -n 1 | tr -d '[:space:]')
    [[ $available =~ ^[0-9]+$ ]] || fail 'Cannot determine free bytes on /data/files.'
    (( available > RESERVE_BYTES )) || fail '/data/files must have MORE than 5 GiB available; free space or enlarge it manually. Nothing will be created.'
    printf 'Mounted data volume: %s bytes available; 5 GiB minimum reserve.\n' "$available"
}

apt_checked() {
    check_disk
    sudo apt-get --no-remove -o Acquire::https::Verify-Peer=true \
        -o Acquire::https::Verify-Host=true -o APT::Get::AllowUnauthenticated=false \
        -o Acquire::AllowInsecureRepositories=false "$@"
}

check_docker_repo() {
    local path matches
    for path in /etc/apt/keyrings/docker.asc /etc/apt/keyrings/docker.gpg \
        /etc/apt/sources.list.d/docker.list /etc/apt/sources.list.d/docker.sources; do
        if sudo test -e "$path" || sudo test -L "$path"; then
            fail "Existing $path will not be overwritten. Have an administrator review/complete Docker installation manually."
        fi
    done
    if matches=$(sudo grep -rlE --include='*.list' --include='*.sources' 'download\.docker\.com' /etc/apt); then
        fail "Existing Docker apt configuration found: $matches. Review manually; setup never replaces repositories."
    else
        [[ $? -eq 1 ]] || fail 'Cannot inspect existing apt repositories.'
    fi
}

install_docker() (
    # Subshell scopes the temporary download cleanup; never execute downloaded code.
    local tool packages package status distro codename arch tmp
    for tool in apt-get dpkg dpkg-query mktemp; do
        command -v "$tool" >/dev/null || fail "Missing $tool; install Docker manually."
    done
    [[ -r /etc/os-release ]] || fail 'Cannot identify the OS; install Docker manually.'
    . /etc/os-release
    distro=${ID:-}
    codename=${VERSION_CODENAME:-}
    case "$distro:$codename" in
        ubuntu:jammy|ubuntu:noble|ubuntu:resolute|debian:bookworm|debian:trixie) ;;
        *) fail 'Automatic installation supports only Ubuntu jammy/noble/resolute or Debian bookworm/trixie.' ;;
    esac
    arch=$(dpkg --print-architecture)
    [[ $arch =~ ^[a-z0-9]+$ ]] || fail 'Cannot determine apt architecture.'
    packages=$(dpkg-query -W -f='${binary:Package} ${db:Status-Status}\n')
    while read -r package status; do
        [[ $status != not-installed && $status != config-files ]] || continue
        case "${package%%:*}" in
            docker|docker.io|docker-engine|docker-doc|docker-compose*|podman-docker|containerd|containerd.io|runc|docker-ce*|docker-buildx-plugin|docker-scan-plugin|moby-*)
                fail "Existing/conflicting package $package ($status). Ask an administrator to review it; setup never removes packages or replaces an engine." ;;
        esac
    done <<< "$packages"
    # Also refuse unmanaged runtimes; their workloads may be unrelated to Docker.
    for tool in containerd runc dockerd; do
        if command -v "$tool" >/dev/null || sudo test -x "/usr/bin/$tool" || sudo test -x "/usr/local/bin/$tool"; then
            fail "Existing $tool runtime found; review Docker installation manually. Nothing will be removed."
        fi
    done
    check_docker_repo
    step "Install Docker from the official signed $distro $codename apt repository"
    apt_checked update
    apt_checked install -y ca-certificates curl
    tmp=$(mktemp -d)
    trap 'rm -rf -- "$tmp"' EXIT
    # Ignore user curl configuration (which could otherwise disable TLS checks).
    curl --disable --fail --show-error --silent --location --proto '=https' --proto-redir '=https' \
        --tlsv1.2 "https://download.docker.com/linux/$distro/gpg" -o "$tmp/docker.asc"
    [[ -s $tmp/docker.asc ]] || fail 'Docker signing key download is empty.'
    printf 'Types: deb\nURIs: https://download.docker.com/linux/%s\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' \
        "$distro" "$codename" "$arch" > "$tmp/docker.sources"
    check_disk
    check_docker_repo
    for tool in /etc/apt/keyrings /etc/apt/sources.list.d; do
        [[ ! -L $tool ]] || fail "Refusing symlink: $tool"
        if ! sudo test -d "$tool"; then sudo install -d -m 0755 "$tool"; fi
    done
    sudo install -m 0644 "$tmp/docker.asc" /etc/apt/keyrings/docker.asc
    sudo install -m 0644 "$tmp/docker.sources" /etc/apt/sources.list.d/docker.sources
    apt_checked update
    apt_checked install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
)

check_containers_and_port() {
    local id project service config binding listeners line own_port=false
    local ids
    ids=$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT")
    for id in $ids; do
        config=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.config_files"}}' "$id")
        service=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$id")
        [[ $config == "$DEPLOY_DIR/compose.yaml" && ($service == qdrant || $service == worker) ]] ||
            fail "Project name $PROJECT is already used by another deployment ($id). Resolve manually."
        if [[ $service == qdrant ]]; then
            [[ $(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/qdrant/storage"}}{{.Source}}{{end}}{{end}}' "$id") == "$ROOT/qdrant" ]] ||
                fail "Existing Qdrant $id uses different storage; refusing to replace it."
        fi
    done
    ids=$(docker ps -q)
    for id in $ids; do
        binding=$(docker inspect -f '{{range $port, $bindings := .NetworkSettings.Ports}}{{if eq $port "6333/tcp"}}{{range $bindings}}{{if eq .HostPort "6333"}}{{.HostIp}}:{{.HostPort}}{{println}}{{end}}{{end}}{{end}}{{end}}' "$id")
        # Also catch containers publishing a DIFFERENT internal port as host 6333.
        if docker inspect -f '{{range .NetworkSettings.Ports}}{{range .}}{{if eq .HostPort "6333"}}occupied{{end}}{{end}}{{end}}' "$id" | grep -q occupied; then
            project=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$id")
            service=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$id")
            [[ $project == "$PROJECT" && $service == qdrant && $binding == 127.0.0.1:6333 ]] ||
                fail "Host port 6333 belongs to another container/binding ($id). Nothing will be stopped."
            [[ $(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/qdrant/storage"}}{{.Source}}{{end}}{{end}}' "$id") == "$ROOT/qdrant" ]] ||
                fail "Existing Qdrant $id uses different storage; refusing to replace it."
            own_port=true
        fi
    done
    listeners=$(sudo ss -H -ltnp 'sport = :6333')
    while IFS= read -r line; do
        [[ -n $line ]] || continue
        # With userland-proxy disabled Docker has no host listening process.
        # Fail closed on any unidentified host process, including host networking.
        [[ $own_port == true && $line == *127.0.0.1:6333* && $line == *'"docker-proxy"'* ]] ||
            fail "A host listener owns port 6333: $line. Resolve manually; it will not be stopped."
    done <<< "$listeners"
}

ensure_directory() {
    local path=$1 owner=$2 mode=$3
    [[ ! -L $path ]] || fail "Refusing symlink: $path"
    if sudo test -e "$path"; then
        sudo test -d "$path" || fail "Not a directory: $path"
        [[ $(sudo stat -c '%u:%g' "$path") == "$owner" ]] ||
            fail "Existing $path must be owned by $owner. Review manually; setup never chowns existing files."
        [[ $(sudo stat -c '%a' "$path") == "$mode" ]] ||
            fail "Existing $path must have mode $mode. Review manually; setup never chmods existing files."
    else
        sudo install -d -m "$mode" -o "${owner%:*}" -g "${owner#*:}" -- "$path"
    fi
}

main() {
    local install_requested=false
    if [[ $# -eq 1 && $1 == --install-docker ]]; then
        install_requested=true
    elif [[ $# -ne 0 ]]; then
        fail 'Usage: bash ingestion/deploy/setup.sh [--install-docker]'
    fi
    [[ $(uname -s) == Linux && $EUID -ne 0 ]] || fail 'Run on the Linux VM as your normal login user, not with sudo.'
    local tool endpoint security image_user docker_binary
    for tool in mountpoint readlink df tail tr sudo ss stat install grep; do
        command -v "$tool" >/dev/null || fail "Missing $tool. Install prerequisites manually; see ingestion/deploy/VM.md."
    done
    check_disk
    [[ -f "$REPO_ROOT/ingestion/catalogue.py" || -f "$REPO_ROOT/ingestion/catalogue/__main__.py" ]] ||
        fail 'Incomplete checkout: ingestion.catalogue is missing. Restore the worker implementation before setup.'
    if ! docker_binary=$(type -P docker); then
        [[ $install_requested == true ]] || fail 'Docker is missing. Install it manually or rerun with --install-docker on a supported Ubuntu/Debian VM.'
        sudo -v
        install_docker
        docker_binary=$(type -P docker) || fail 'Installation did not provide the Docker CLI; review apt output manually.'
    fi
    DOCKER=("$docker_binary")
    docker compose version >/dev/null || fail 'Docker exists but Compose v2 is missing. Have an administrator install the compatible Compose plugin using the official Docker instructions; setup will not replace the engine, even with --install-docker.'
    # DOCKER_CONTEXT takes precedence over DOCKER_HOST in the Docker CLI.
    if [[ -n ${DOCKER_CONTEXT:-} ]]; then
        endpoint=$(docker context inspect "$DOCKER_CONTEXT" -f '{{.Endpoints.docker.Host}}')
    else
        endpoint=${DOCKER_HOST:-$(docker context inspect -f '{{.Endpoints.docker.Host}}')}
    fi
    [[ $endpoint == unix:///var/run/docker.sock ]] || fail 'Use the local rootful Docker engine at unix:///var/run/docker.sock, not a remote/rootless context.'
    sudo -v
    if [[ -d /run/systemd/system ]] && command -v systemctl >/dev/null; then
        step 'Enable the existing Docker service (does not install Docker)'
        sudo systemctl enable --now docker
    fi
    if ! docker info >/dev/null 2>&1; then
        # Do not add the login user to the root-equivalent docker group. Pin the
        # endpoint so sudo cannot silently select root's remote/rootless context.
        DOCKER=(sudo env -u DOCKER_CONTEXT -u DOCKER_HOST "$docker_binary" --host=unix:///var/run/docker.sock)
        docker info >/dev/null 2>&1 || fail 'Local Docker is unavailable even with sudo. Have the administrator start/check the rootful engine; do not run all of setup as root.'
        docker compose version >/dev/null || fail 'Compose v2 is unavailable under sudo. Have an administrator install the Compose plugin system-wide for this engine; setup will not replace Docker.'
    fi
    security=$(docker info -f '{{json .SecurityOptions}}')
    [[ $security != *userns* && $security != *rootless* ]] || fail 'This fixed-UID deployment requires rootful Docker without user namespace remapping.'
    "${COMPOSE[@]}" config --quiet
    check_containers_and_port

    step 'Prepare only dedicated directories on the verified data mount'
    check_disk
    ensure_directory "$ROOT" 0:0 755
    ensure_directory "$ROOT/pipeline" 10001:10001 700
    ensure_directory "$ROOT/models" 10001:10001 700
    # Official v1.19.0 Dockerfile defaults USER_ID=0 (USER 0:0).
    ensure_directory "$ROOT/qdrant" 0:0 700

    step 'Pull Qdrant and build the ingestion-only worker from this checkout'
    "${COMPOSE[@]}" pull qdrant
    image_user=$(docker image inspect qdrant/qdrant:v1.19.0 -f '{{.Config.User}}')
    [[ $image_user == 0:0 || $image_user == 0 || $image_user == root || -z $image_user ]] ||
        fail "Unexpected Qdrant image default user '$image_user'; review storage ownership before proceeding."
    "${COMPOSE[@]}" build worker
    # Verify the implemented CLI without importing/installing on host.
    "${COMPOSE[@]}" run --rm --no-deps --entrypoint python worker -m ingestion.catalogue --help
    check_disk
    check_containers_and_port
    step 'Start Qdrant, explicitly check readiness, then start the worker'
    "${COMPOSE[@]}" up -d qdrant
    "${COMPOSE[@]}" run --rm --no-deps worker --check-qdrant
    "${COMPOSE[@]}" up -d --no-deps worker
    "${COMPOSE[@]}" ps
    printf '\nStarted in background. Running is not proof of ingestion progress; inspect catalogue status and logs as described in ingestion/deploy/VM.md.\n'
}

main "$@"