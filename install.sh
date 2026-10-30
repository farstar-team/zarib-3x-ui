#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

if [[ ${EUID} -ne 0 ]]; then
  printf 'Run this installer as root.\n' >&2
  exit 1
fi
if [[ ! -d /run/systemd/system ]] || ! command -v apt-get >/dev/null; then
  printf 'ZARIB supports systemd-based Debian/Ubuntu servers with an existing 3x-ui panel.\n' >&2
  exit 1
fi

printf '\n  ZARIB.  /  Independent inbound traffic multipliers\n\n'
packages=(python3 python3-cryptography python3-grpcio python3-protobuf protobuf-compiler iptables curl ca-certificates)
missing=()
for package in "${packages[@]}"; do
  if ! dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q '^install ok installed$'; then
    missing+=("$package")
  fi
done
if ! command -v psql >/dev/null; then missing+=(postgresql-client); fi
if (( ${#missing[@]} )); then
  export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
  apt-get -q update
  apt-get -q -y --no-install-recommends install "${missing[@]}"
fi
python3 -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10 or newer is required"'

# A local source override is useful for offline installs and integration tests.
# Normal one-command installation fetches exactly one complete GitHub archive.
if [[ -n ${ZARIB_SOURCE_DIR:-} ]]; then
  source_dir=$(realpath -- "$ZARIB_SOURCE_DIR")
else
  staging=$(mktemp -d /tmp/zarib-install.XXXXXXXX)
  cleanup() {
    if [[ $staging == /tmp/zarib-install.* && -d $staging ]]; then
      rm -rf -- "$staging"
    fi
  }
  trap cleanup EXIT
  curl --fail --show-error --silent --location --retry 2 --connect-timeout 15 \
    'https://codeload.github.com/farstar-team/zarib-3x-ui/tar.gz/refs/heads/main' \
    --output "$staging/source.tar.gz"
  tar --no-same-owner -xzf "$staging/source.tar.gz" -C "$staging"
  source_dir="$staging/zarib-3x-ui-main"
fi
if [[ ! -f $source_dir/installer.py || ! -f $source_dir/SHA256SUMS ]]; then
  printf 'The downloaded source package is incomplete.\n' >&2
  exit 1
fi
(cd "$source_dir" && sha256sum --check --quiet SHA256SUMS)
python3 "$source_dir/installer.py" "$@"
