"""Resemble Enhance runner: executed with tools/resemble-venv's Python, never imported by soundlab.

Usage: python -I resemble_worker.py IN.wav OUT.wav MODEL_DIR TAU NFE LAMBD SOLVER SEED

Writes JSON lines to stdout: {"progress": 0..1} while it works. Runs on the CPU — full enhance
crashes on Apple's MPS backend. The rebuild starts from random noise: the seed picks it, so the same
settings + seed give the same output (seeded after the model load, which uses the RNG too).
"""

import json
import math
import sys
from pathlib import Path

CHUNK_SECONDS, OVERLAP_SECONDS = 30.0, 1.0  # resemble_enhance.inference defaults


def emit(**msg) -> None:
    print(json.dumps(msg), flush=True)


def main() -> int:
    src, out, model_dir = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    tau, nfe, lambd, solver = float(sys.argv[4]), int(sys.argv[5]), float(sys.argv[6]), sys.argv[7]
    seed = int(sys.argv[8])

    emit(progress=0.0)
    import torch
    import torchaudio
    from resemble_enhance.enhancer.inference import enhance, load_enhancer
    from resemble_enhance.enhancer.lcfm.cfm import Solver

    wav, sr = torchaudio.load(str(src))
    wav = wav.mean(0)

    # Progress = solver steps done / all steps (n_steps per 30 s chunk); the model load before the
    # first step and the vocoder after the last one aren't counted.
    seconds = wav.shape[-1] / sr
    chunks = max(1, math.ceil(seconds / (CHUNK_SECONDS - OVERLAP_SECONDS)))  # = its range(0, n, hop)
    probe = Solver(method=solver, nfe=nfe)
    total = max(1, probe.n_steps * chunks)
    done = 0

    def counted(step):  # the step functions are staticmethods
        def wrapper(*args, **kwargs):
            nonlocal done
            result = step(*args, **kwargs)
            done += 1
            emit(progress=round(min(done / total, 1.0), 3))
            return result
        return staticmethod(wrapper)

    for name in ("_euler_step", "_midpoint_step", "_rk4_step"):
        setattr(Solver, name, counted(getattr(Solver, name)))

    load_enhancer(model_dir, "cpu")  # cached: enhance() reuses it
    torch.manual_seed(seed)
    hwav, out_sr = enhance(wav, sr, "cpu", nfe=nfe, solver=solver, lambd=lambd, tau=tau, run_dir=model_dir)
    torchaudio.save(str(out), hwav[None].cpu().float(), out_sr, encoding="PCM_F", bits_per_sample=32)
    emit(progress=1.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
