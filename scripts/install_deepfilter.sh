#!/usr/bin/env bash
# Downloads the DeepFilterNet3 Rust CLI binary into tools/.
set -euo pipefail

VERSION="0.5.6"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/tools/deep-filter"

case "$(uname -s)-$(uname -m)" in
  Darwin-arm64)  TARGET="aarch64-apple-darwin" ;;
  Darwin-x86_64) TARGET="x86_64-apple-darwin" ;;
  *) echo "Unsupported platform: $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac

URL="https://github.com/Rikorose/DeepFilterNet/releases/download/v${VERSION}/deep-filter-${VERSION}-${TARGET}"

mkdir -p "$ROOT/tools"
echo "Downloading: $URL"
curl -fL --progress-bar -o "$DEST.tmp" "$URL"
chmod +x "$DEST.tmp"
mv "$DEST.tmp" "$DEST"
# macOS may quarantine the downloaded binary
xattr -d com.apple.quarantine "$DEST" 2>/dev/null || true

"$DEST" --version
echo "Installed: $DEST"
