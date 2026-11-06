#!/usr/bin/env bash
# Share bounded dependency setup across every GKI and OPlus matrix worker.
set -euo pipefail

profile="${1:-gki}"
case "$profile" in
  gki)
    packages=(ccache python3 python3-cryptography git curl build-essential
      libssl-dev bison flex libelf-dev dwarves lz4)
    ;;
  oplus)
    packages=(aria2 bc bison flex make gcc g++ binutils python-is-python3
      libssl-dev libelf-dev libdw-dev zlib1g-dev ccache lz4 zstd xz-utils
      zip unzip patch curl wget rsync)
    ;;
  *) echo "Unknown build dependency profile: $profile" >&2; exit 2 ;;
esac

find_missing_packages() {
  missing=()
  for package in "${packages[@]}"; do
    if [[ "$(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true)" != installed ]]; then
      missing+=("$package")
    fi
  done
}

find_missing_packages
if ((${#missing[@]} == 0)); then
  echo "$profile dependencies: all required packages are already installed."
  exit 0
fi

echo "$profile dependencies to install: ${missing[*]}"
apt_options=(-o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30
  -o Acquire::Retries=1 -o DPkg::Lock::Timeout=45 -o Dpkg::Use-Pty=0
  -o Acquire::Languages=none)
source_options=()
install_missing() {
  # Run timeout as root so it can also terminate privileged apt/dpkg children.
  sudo timeout --kill-after=10s 180s env DEBIAN_FRONTEND=noninteractive \
    apt-get "${apt_options[@]}" "${source_options[@]}" \
    install -y --no-upgrade --no-install-recommends "${missing[@]}"
}

prepare_official_indexes() {
  local distro codename architecture archive security
  read -r distro codename <<< "$(. /etc/os-release; printf '%s %s\n' "${ID:-}" "${VERSION_CODENAME:-}")"
  if [[ "$distro" != ubuntu || ! "$codename" =~ ^[a-z]+$ ]]; then
    echo 'An Ubuntu runner with VERSION_CODENAME is required for dependency recovery.' >&2
    return 1
  fi
  architecture="$(dpkg --print-architecture)"
  case "$architecture" in
    amd64|i386)
      archive=https://archive.ubuntu.com/ubuntu
      security=https://security.ubuntu.com/ubuntu
      ;;
    arm64|armhf|ppc64el|riscv64|s390x)
      archive=https://ports.ubuntu.com/ubuntu-ports
      security="$archive"
      ;;
    *) echo "Unsupported Ubuntu package architecture: $architecture" >&2; return 1 ;;
  esac

  # Do not edit the runner's source lists or delete its usable package indexes.
  # An isolated official HTTPS source bypasses the Azure mirrorlist and unrelated
  # Chrome/Microsoft repositories that blocked the original unconditional update.
  apt_directory="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/gki-build-deps.XXXXXX")"
  chmod 755 "$apt_directory"
  mkdir -p "$apt_directory/sources.list.d" "$apt_directory/lists/partial"
  cat > "$apt_directory/ubuntu.sources" <<EOF
Types: deb
URIs: $archive
Suites: $codename ${codename}-updates ${codename}-backports
Components: main restricted universe multiverse
Architectures: $architecture
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

Types: deb
URIs: $security
Suites: ${codename}-security
Components: main restricted universe multiverse
Architectures: $architecture
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
EOF
  source_options=(-o "Dir::Etc::sourcelist=$apt_directory/ubuntu.sources"
    -o "Dir::Etc::sourceparts=$apt_directory/sources.list.d"
    -o "Dir::State::lists=$apt_directory/lists"
    -o Dir::Cache::pkgcache= -o Dir::Cache::srcpkgcache=)
}

apt_directory=''
cleanup() {
  # mktemp creates this exact directory; never remove a caller-supplied path.
  if [[ -n "$apt_directory" && "${apt_directory##*/}" == gki-build-deps.* ]]; then
    sudo rm -rf -- "$apt_directory"
  fi
}
trap cleanup EXIT

# Most hosted-runner packages and indexes are already usable. Avoid any index
# refresh when all packages exist or the missing ones install from those indexes.
if ! install_missing; then
  echo 'Initial installation failed; retrying with isolated official Ubuntu indexes (90-second refresh limit).'
  prepare_official_indexes
  if ! sudo timeout --kill-after=10s 90s env DEBIAN_FRONTEND=noninteractive \
      apt-get "${apt_options[@]}" "${source_options[@]}" -o APT::Update::Error-Mode=any update; then
    echo '::warning::Official index refresh failed or timed out; the final installation must still succeed and every dependency will be verified.'
  fi
  # Refresh may partially succeed. Accept it only if apt installs every missing
  # package successfully; a failed final install retains its nonzero exit status.
  install_missing
fi

find_missing_packages
if ((${#missing[@]} != 0)); then
  echo "Required $profile dependencies are still missing: ${missing[*]}" >&2
  exit 1
fi
echo "$profile dependencies: all required packages verified."
