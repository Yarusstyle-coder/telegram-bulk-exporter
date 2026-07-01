#!/usr/bin/env bash
# Download the pinned tdl release binary into tools/tdl/ (Linux / macOS).
#
# tdl (https://github.com/iyear/tdl) is the multi-threaded media downloader this
# exporter shells out to. It is AGPL-3.0 and NOT vendored into this repo
# (tools/tdl/ is gitignored). This script fetches the pinned upstream release for
# your platform, verifies its SHA-256 against the published checksums, and
# extracts the tdl binary into tools/tdl/.
#
# Re-run it any time; it overwrites tools/tdl/tdl. Bump VERSION to update.
# Keep VERSION in sync with scripts/fetch_tdl.ps1.
set -euo pipefail

VERSION="${TDL_VERSION:-0.20.2}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dest_dir="$repo_root/tools/tdl"
dest_bin="$dest_dir/tdl"

case "$(uname -s)" in
    Linux)  os="Linux" ;;
    Darwin) os="MacOS" ;;
    *) echo "Unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac

case "$(uname -m)" in
    x86_64|amd64)   arch="64bit" ;;
    aarch64|arm64)  arch="arm64" ;;
    i386|i686)      arch="32bit" ;;
    *) echo "Unsupported CPU architecture: $(uname -m)" >&2; exit 1 ;;
esac

asset="tdl_${os}_${arch}.tar.gz"
base="https://github.com/iyear/tdl/releases/download/v${VERSION}"

echo "Fetching tdl v${VERSION} (${asset})..."

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

curl -fSL "$base/$asset" -o "$tmp/$asset"
curl -fSL "$base/tdl_checksums.txt" -o "$tmp/tdl_checksums.txt"

# Verify SHA-256 against the published checksums file (lines: "<hash>  <name>").
expected="$(awk -v a="$asset" '$2==a {print $1}' "$tmp/tdl_checksums.txt")"
if [ -z "$expected" ]; then
    echo "Checksum for $asset not found in tdl_checksums.txt" >&2
    exit 1
fi
if command -v sha256sum >/dev/null 2>&1; then
    actual="$(sha256sum "$tmp/$asset" | awk '{print $1}')"
else
    actual="$(shasum -a 256 "$tmp/$asset" | awk '{print $1}')"
fi
if [ "$actual" != "$expected" ]; then
    echo "SHA-256 mismatch for $asset" >&2
    echo "  expected: $expected" >&2
    echo "  actual:   $actual" >&2
    exit 1
fi
echo "Checksum OK ($expected)"

mkdir -p "$dest_dir"
tar -xzf "$tmp/$asset" -C "$tmp"
src_bin="$(find "$tmp" -type f -name tdl | head -n1)"
if [ -z "$src_bin" ]; then
    echo "tdl binary not found inside $asset" >&2
    exit 1
fi
install -m 0755 "$src_bin" "$dest_bin"
echo "Installed: $dest_bin"
"$dest_bin" version
