"""A/B comparison: processes one recording stage by stage into loudness-matched variants."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from .pipeline import EnhanceOptions, enhance, find_resemble, resolve_outputs

# (file name, description, option changes) — all at the same target loudness, WAV
VARIANTS = [
    ("1_raw", "no processing (HPF + loudness only)", dict(denoiser="none", preset="flat")),
    ("2_default", "default: DeepFilterNet 30 dB + post-filter, no EQ", dict(denoiser="deepfilter", preset="flat")),
    ("3_dfn-max", "DeepFilterNet unlimited suppression (100 dB) + post-filter",
     dict(denoiser="deepfilter", atten_lim_db=100.0, preset="flat")),
    ("4_default_voice-light", "default + light EQ/comp", dict(denoiser="deepfilter", preset="voice-light")),
    ("5_default_voice-a", "default + voice-a EQ/comp (old default)", dict(denoiser="deepfilter", preset="voice-a")),
]
# Only when installed (scripts/install_resemble.sh); slow — a few times the recording's length
RESEMBLE_VARIANT = ("6_resemble", "Resemble Enhance, full (default settings), no EQ",
                    dict(denoiser="resemble", preset="flat"))


def compare(src: Path, base: EnhanceOptions | None = None, log: Callable[[str], None] = print) -> Path:
    base = replace(base or EnhanceOptions(), fmt="wav")
    src = Path(src).expanduser().resolve()
    now = datetime.now()
    out_dir = resolve_outputs(base) / now.strftime("%Y-%m-%d") / f"compare_{src.stem}_{now.strftime('%H%M%S')}"
    out_dir.mkdir(parents=True)

    lines = [f"Source: {src}", f"Target: {base.target_i} LUFS (all variants loudness-matched)", ""]
    variants = VARIANTS + ([RESEMBLE_VARIANT] if find_resemble() else [])
    for name, desc, changes in variants:
        log(f"  • {name}: {desc}")
        r = enhance(src, replace(base, **changes), log=lambda _: None, out=out_dir / f"{name}.wav")
        snr = r.data["noise_output"]["snr_db"]
        lines.append(f"{name}.wav — {desc} (SNR {snr} dB)")
    (out_dir / "README.txt").write_text("\n".join(lines) + "\n")
    return out_dir
