"""Sidon runner: executed with tools/sidon-venv's Python, never imported by soundlab.

Usage: python -I sidon_worker.py IN.wav OUT.wav MODEL_DIR

Writes JSON lines to stdout: {"progress": 0..1} while it works. Same steps as the official demo
(huggingface.co/spaces/sarulab-speech/sidon_demo_beta): peak-normalise, 50 Hz high-pass, 16 kHz into
the w2v-BERT feature predictor, 96 s chunks, the DAC decoder writes 48 kHz. Everything loads from
MODEL_DIR (the caller sets HF_HUB_OFFLINE): no network. CPU only — the CPU TorchScript files have the
device baked in, so they fail on MPS. Deterministic: no settings, no seed.
"""

import json
import sys

CHUNK_SECONDS = 96  # the demo's chunk length
LOAD_SHARE = 0.1  # share of the progress bar for loading the models


def emit(**msg) -> None:
    print(json.dumps(msg), flush=True)


def main() -> int:
    src, out, model_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    emit(progress=0.0)
    import numpy as np
    import soundfile as sf
    import torch
    import torch.nn.functional as F
    import torchaudio
    import transformers

    fe = torch.jit.load(f"{model_dir}/feature_extractor_cpu.pt", map_location="cpu")
    decoder = torch.jit.load(f"{model_dir}/decoder_cpu.pt", map_location="cpu")
    preprocessor = transformers.SeamlessM4TFeatureExtractor.from_pretrained(model_dir)
    emit(progress=LOAD_SHARE)

    x, sr = sf.read(src, dtype="float32", always_2d=True)
    x = x.mean(1)
    peak = float(np.abs(x).max())
    if peak <= 0:
        raise SystemExit("the input is silent")
    target = int(48_000 / sr * len(x))
    with torch.inference_mode():
        wav = torch.from_numpy(0.9 * x / peak).view(1, -1)
        wav = torchaudio.functional.highpass_biquad(wav, sr, 50)
        wav16 = F.pad(torchaudio.functional.resample(wav, sr, 16_000), (0, 24000))
        chunks = wav16.view(-1).split(16000 * CHUNK_SECONDS)
        restored, cache = [], None
        for i, chunk in enumerate(chunks):
            inputs = preprocessor(F.pad(chunk, (160, 160)), return_tensors="pt", sampling_rate=16000)
            feature = fe(inputs["input_features"])["last_hidden_state"]
            if cache is not None:
                feature = torch.cat([cache, feature], dim=1)
            restored.append(decoder(feature.transpose(1, 2)).view(-1)[:-960])
            cache = feature[:, -1:]
            emit(progress=round(LOAD_SHARE + (1 - LOAD_SHARE) * (i + 1) / len(chunks), 3))
        y = torch.cat(restored).numpy()[:target] * (peak / 0.9)  # back to the input's level
    sf.write(out, y, 48_000, subtype="FLOAT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
