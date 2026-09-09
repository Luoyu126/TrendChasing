#!/usr/bin/env bash
# Official instructions: https://docs.docker.com/engine/install/ubuntu/
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo 'Run this script with sudo in your own terminal.' >&2
    exit 1
fi

source /etc/os-release
if [[ ${ID:-} != ubuntu ]]; then
    echo 'This installer supports Ubuntu only.' >&2
    exit 1
fi
case ${VERSION_CODENAME:-} in
    resolute|noble|jammy) ;;
    *) echo "Unsupported Ubuntu release: ${VERSION_CODENAME:-unknown}" >&2; exit 1 ;;
esac
if [[ ! -d /run/systemd/system ]]; then
    echo 'Enable systemd before running this installer.' >&2
    exit 1
fi

# Do not remove an existing container installation automatically.
for package in docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc; do
    if [[ $(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true) == 'install ok installed' ]]; then
        echo "Conflicting package already installed: $package. Review it before proceeding." >&2
        exit 1
    fi
done

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl --fail --show-error --silent --location --retry 3 \
    https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod 0644 /etc/apt/keyrings/docker.asc

docker_source=$(mktemp)
trap 'rm -f "$docker_source"' EXIT
cat > "$docker_source" <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
if [[ -e /etc/apt/sources.list.d/docker.sources ]] && ! cmp -s "$docker_source" /etc/apt/sources.list.d/docker.sources; then
    echo 'Existing docker.sources differs; review it before replacing it.' >&2
    exit 1
fi
install -m 0644 "$docker_source" /etc/apt/sources.list.d/docker.sources
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
docker version
docker compose version
docker run --rm hello-world
echo 'Docker Engine and Compose installed and verified. Use sudo docker for subsequent commands.'
