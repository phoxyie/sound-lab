#!/usr/bin/env bash
# Installs Sidon (optional generative denoiser) into tools/: its own venv + the model files.
# ~2 GB (PyTorch ~0.9 GB, models 1 GB). Safe to re-run: what's already there is kept.
# Needs Python 3.11+ (PyTorch 2.15 wheels); set PYTHON=/path/to/python to pick one.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/tools/sidon-venv"
MODEL="$ROOT/tools/sidon-model"
# Hugging Face commits, pinned so a reinstall gets the same files: Sidon v0.1 (2025-12-15, MIT) and
# the w2v-BERT 2.0 feature-extractor settings it uses (2024-01-25)
SIDON="https://huggingface.co/sarulab-speech/sidon-v0.1/resolve/b3b02d8bbd55fdbc410e6e46e76ef95ace4fbf52"
W2V="https://huggingface.co/facebook/w2v-bert-2.0/resolve/da985ba0987f70aaeb84a80f2851cfac8c697a7b"
FILES=(
  "$SIDON/feature_extractor_cpu.pt fd9abc906a9048b3c047bd3d246a25f6485d09c2d6bb85e098f527f844efc019"
  "$SIDON/decoder_cpu.pt 34dcc80fab75bd1336369ba5b2063f6da475887aa824db94b495c4059c5fdea4"
  "$W2V/preprocessor_config.json 8e6281aad64f97e40534135a59dcc5d33571efae376f2a25adf5551951897ab4"
)
DEPS=(torch==2.15.0 torchaudio==2.11.0 transformers==5.22.0 soundfile==0.14.0 numpy==2.4.6)

python_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)' 2>/dev/null; }

if [ ! -x "$VENV/bin/python" ]; then
  PY=""
  for c in "${PYTHON:-}" python3.13 python3.12 python3.11 python3; do
    if [ -n "$c" ] && command -v "$c" >/dev/null && python_ok "$c"; then PY="$c"; break; fi
  done
  if [ -z "$PY" ]; then
    echo "Python 3.11+ not found (PyTorch 2.15 has no wheels for older ones)." >&2
    echo "Install one (e.g. brew install python@3.12) or set PYTHON=/path/to/python3.12" >&2
    exit 1
  fi
  echo "Creating venv with $("$PY" --version): $VENV"
  "$PY" -m venv "$VENV"
fi

PIP=("$VENV/bin/pip" install --quiet --disable-pip-version-check)
echo "Installing packages (PyTorch is ~0.9 GB the first time)…"
"${PIP[@]}" --upgrade pip
"${PIP[@]}" "${DEPS[@]}"

mkdir -p "$MODEL"
for entry in "${FILES[@]}"; do
  read -r url sha <<<"$entry"
  dest="$MODEL/$(basename "$url")"
  if [ -f "$dest" ] && [ "$(shasum -a 256 "$dest" | cut -d' ' -f1)" = "$sha" ]; then
    continue
  fi
  echo "Downloading: $(basename "$url")"
  curl -fL --progress-bar -o "$dest.tmp" "$url"
  if [ "$(shasum -a 256 "$dest.tmp" | cut -d' ' -f1)" != "$sha" ]; then
    rm -f "$dest.tmp"
    echo "Checksum mismatch for $(basename "$url") — download aborted" >&2
    exit 1
  fi
  mv "$dest.tmp" "$dest"
done

# Runs offline: everything is loaded from tools/sidon-model
HF_HUB_OFFLINE=1 "$VENV/bin/python" -I -c "import torch, torchaudio, transformers" >/dev/null 2>&1
echo "Installed: $VENV + $MODEL"
echo "Check with: .venv/bin/soundlab doctor"
