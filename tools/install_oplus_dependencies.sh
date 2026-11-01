#!/usr/bin/env bash
# Install only missing runner packages, with bounded network and lock waits.
set -euo pipefail

packages=(aria2 bc bison flex make gcc g++ binutils python-is-python3
  libssl-dev libelf-dev libdw-dev zlib1g-dev ccache lz4 zstd xz-utils
  zip unzip patch curl wget rsync)
missing=()
for package in "${packages[@]}"; do
  if [[ "$(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true)" != installed ]]; then
    missing+=("$package")
  fi
done
if ((${#missing[@]} == 0)); then
  echo 'OPlus dependencies: all required packages are already installed.'
  exit 0
fi

echo "OPlus dependencies to install: ${missing[*]}"
apt_options=(-o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30
  -o Acquire::Retries=1 -o DPkg::Lock::Timeout=45 -o Dpkg::Use-Pty=0)
install_missing() {
  timeout --kill-after=10s 180s sudo env DEBIAN_FRONTEND=noninteractive \
    apt-get "${apt_options[@]}" install -y --no-upgrade --no-install-recommends "${missing[@]}"
}
# Runner images normally already have usable package indexes. Avoid refreshing
# every third-party repository on every matrix job before even trying them.
if ! install_missing; then
  echo 'Initial package installation failed; refreshing indexes once (90-second limit).'
  if ! timeout --kill-after=10s 90s sudo env DEBIAN_FRONTEND=noninteractive \
      apt-get "${apt_options[@]}" update; then
    echo 'Package index refresh did not complete; retrying with the available indexes.'
  fi
  install_missing
fi
for package in "${packages[@]}"; do
  if [[ "$(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true)" != installed ]]; then
    echo "Required OPlus dependency is still missing: $package" >&2
    exit 1
  fi
done
echo 'OPlus dependencies: all required packages verified.'
