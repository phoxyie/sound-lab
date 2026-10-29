#!/usr/bin/env bash
# Downloads the DeepFilterNet3 Rust CLI binary into tools/ and checks its SHA-256 before running it.
set -euo pipefail

VERSION="0.5.6"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/tools/deep-filter"

# GitHub publishes no checksums for this release: these are the hashes of the files as downloaded on
# 2026-10-28 / 2026-10-30 (the arm64 one unchanged in between), so a later swap or a broken download
# is caught before the binary runs
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64)  TARGET="aarch64-apple-darwin"; SHA="4601e7f4e4c03e59a4c5b5000216ef3add3e808799cfccd95e14e83ea4611081" ;;
  Darwin-x86_64) TARGET="x86_64-apple-darwin";  SHA="d3be84003acb7c23e738ad7f70a158ec779a8d233a82e7fa3e717d112eb5b50f" ;;
  *) echo "Unsupported platform: $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac

URL="https://github.com/Rikorose/DeepFilterNet/releases/download/v${VERSION}/deep-filter-${VERSION}-${TARGET}"

mkdir -p "$ROOT/tools"
echo "Downloading: $URL"
curl -fL --progress-bar -o "$DEST.tmp" "$URL"
if [ "$(shasum -a 256 "$DEST.tmp" | cut -d' ' -f1)" != "$SHA" ]; then
  rm -f "$DEST.tmp"
  echo "Checksum mismatch — the downloaded file isn't the expected deep-filter ${VERSION}; not installed" >&2
  exit 1
fi
chmod +x "$DEST.tmp"
mv "$DEST.tmp" "$DEST"
# macOS may quarantine the downloaded binary
xattr -d com.apple.quarantine "$DEST" 2>/dev/null || true

"$DEST" --version
echo "Installed: $DEST"
