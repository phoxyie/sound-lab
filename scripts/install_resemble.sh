#!/usr/bin/env bash
# Installs Resemble Enhance (optional denoiser) into tools/: its own venv + the model weights.
# ~1.6 GB (PyTorch ~0.9 GB, weights 713 MB). Safe to re-run: what's already there is kept.
# Needs Python 3.10 or 3.11 (PyTorch 2.1.1 wheels); set PYTHON=/path/to/python to pick one.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/tools/resemble-venv"
MODEL="$ROOT/tools/resemble-model/enhancer_stage2"
# Hugging Face model commit (2023-12-21), pinned so a reinstall gets the same weights
HF="https://huggingface.co/ResembleAI/resemble-enhance/resolve/4e3510ce4a8391159f665903544c5150bee7b2cb/enhancer_stage2"
FILES=(
  "hparams.yaml 80c3f15bc5a5b2cacf2c698699a0f6599d62911c0d53e1d6dee895c0d7cbaeac"
  "ds/G/latest 37a8eec1ce19687d132fe29051dca629d164e2c4958ba141d5f4133a33f0688f"
  "ds/G/default/mp_rank_00_model_states.pt f9d035f318de3e6d919bc70cf7ad7d32b4fe92ec5cbe0b30029a27f5db07d9d6"
)
# resemble-enhance 0.0.1's own pins, minus gradio and torchvision (only its web demo needs them)
DEPS=(
  torch==2.1.1 torchaudio==2.1.1 numpy==1.26.2 scipy==1.11.4 librosa==0.10.1 soundfile==0.12.1
  resampy==0.4.2 omegaconf==2.3.0 tqdm==4.66.1 rich==13.7.0 tabulate==0.8.10 pandas==2.1.3
  matplotlib==3.8.1 celluloid==0.2.0 ptflops==0.7.1.2
)

python_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info[:2] in ((3, 10), (3, 11)) else 1)' 2>/dev/null; }

if [ ! -x "$VENV/bin/python" ]; then
  PY=""
  for c in "${PYTHON:-}" python3.11 python3.10 python3; do
    if [ -n "$c" ] && command -v "$c" >/dev/null && python_ok "$c"; then PY="$c"; break; fi
  done
  if [ -z "$PY" ]; then
    echo "Python 3.10 or 3.11 not found (PyTorch 2.1.1 has no wheels for newer ones)." >&2
    echo "Install one (e.g. brew install python@3.11) or set PYTHON=/path/to/python3.11" >&2
    exit 1
  fi
  echo "Creating venv with $("$PY" --version): $VENV"
  "$PY" -m venv "$VENV"
fi

PIP=("$VENV/bin/pip" install --quiet --disable-pip-version-check)
echo "Installing packages (PyTorch is ~0.9 GB the first time)…"
"${PIP[@]}" --upgrade pip
"${PIP[@]}" "${DEPS[@]}"
# Imported at load time although only training uses it; DS_BUILD_OPS=0 skips its CUDA kernels
DS_BUILD_OPS=0 "${PIP[@]}" deepspeed==0.12.4
# --no-deps: its pins include gradio / torchvision, and deepspeed is handled above
"${PIP[@]}" --no-deps resemble-enhance==0.0.1

for entry in "${FILES[@]}"; do
  read -r rel sha <<<"$entry"
  dest="$MODEL/$rel"
  if [ -f "$dest" ] && [ "$(shasum -a 256 "$dest" | cut -d' ' -f1)" = "$sha" ]; then
    continue
  fi
  echo "Downloading weights: $rel"
  mkdir -p "$(dirname "$dest")"
  curl -fL --progress-bar -o "$dest.tmp" "$HF/$rel"
  if [ "$(shasum -a 256 "$dest.tmp" | cut -d' ' -f1)" != "$sha" ]; then
    rm -f "$dest.tmp"
    echo "Checksum mismatch for $rel — download aborted" >&2
    exit 1
  fi
  mv "$dest.tmp" "$dest"
done

"$VENV/bin/python" -I -c "import resemble_enhance.enhancer.inference" >/dev/null 2>&1
echo "Installed: $VENV + $MODEL"
echo "Check with: .venv/bin/soundlab doctor"
