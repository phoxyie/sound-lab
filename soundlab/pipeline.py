"""Voice enhancement pipeline.

Flow: 80 Hz HPF + 48 kHz mono WAV → denoise (DeepFilterNet3 / afftdn)
→ DSP chain → 2-pass linear loudnorm → outputs/YYYY-MM-DD/
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import projects
from .presets import AFFTDN_FALLBACK, PRESETS

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_RATE = 48000
LIMITER_HEADROOM_DB = 1.0  # limiter ceiling = TP target - this value
LIMITER_MAX_ITER = 4
TP_MARGIN_DB = 0.2  # TP margin against measurement differences
NOISE_FLOOR_MIN_DB = -120.0

CODECS = {
    "m4a": ["-c:a", "aac", "-b:a", "192k"],
    "wav": ["-c:a", "pcm_s24le"],
}


@dataclass
class EnhanceOptions:
    preset: str = "flat"  # ear test: full DeepFilterNet without EQ sounded best
    denoiser: str = "auto"  # auto | deepfilter | afftdn | none
    atten_lim_db: float = 30.0
    postfilter: bool = True
    target_i: float = -16.0
    target_tp: float = -1.5
    target_lra: float = 11.0
    fmt: str = "m4a"
    outputs_root: Path | None = None  # None → the current project's outputs/ folder


@dataclass
class EnhanceResult:
    output: Path
    report: Path
    data: dict


class PipelineError(RuntimeError):
    pass


def resolve_outputs(opts: EnhanceOptions) -> Path:
    return opts.outputs_root or projects.current().outputs


def find_deep_filter() -> str | None:
    local = PROJECT_ROOT / "tools" / "deep-filter"
    if local.is_file():
        return str(local)
    return shutil.which("deep-filter")


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise PipelineError(f"Command failed: {cmd[0]}\n{tail}")
    return proc


def _ffmpeg(*args: str) -> subprocess.CompletedProcess:
    return _run(["ffmpeg", "-hide_banner", "-nostdin", "-y", *args])


def _parse_loudnorm_json(stderr: str) -> dict:
    start, end = stderr.rfind("{"), stderr.rfind("}")
    if start == -1 or end < start:
        raise PipelineError("loudnorm JSON output not found")
    return json.loads(stderr[start : end + 1])


def _loudnorm_filter(opts: EnhanceOptions, measured: dict | None = None) -> str:
    lra = opts.target_lra
    if measured:
        # If the measured LRA exceeds the target, loudnorm gives up linear mode; the compressor
        # handles dynamics, so we stretch the target to the measured value (capped at 50).
        lra = min(max(lra, float(measured["input_lra"]) + 0.1), 50.0)
    f = f"loudnorm=I={opts.target_i}:TP={opts.target_tp}:LRA={lra:g}:print_format=json"
    if measured:
        f += (
            f":measured_I={measured['input_i']}"
            f":measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}"
            f":measured_thresh={measured['input_thresh']}"
            f":offset={measured['target_offset']}"
            ":linear=true"
        )
    return f


def measure_loudness(path: Path) -> dict:
    proc = _ffmpeg("-i", str(path), "-af", "loudnorm=print_format=json", "-f", "null", "-")
    m = _parse_loudnorm_json(proc.stderr)
    return {"lufs": float(m["input_i"]), "true_peak": float(m["input_tp"]), "lra": float(m["input_lra"])}


RMS_WINDOW = 0.02  # seconds


def rms_windows(path: Path) -> list[float]:
    """RMS level (dBFS) of consecutive 20 ms windows, in order."""
    window = round(SAMPLE_RATE * RMS_WINDOW)
    proc = _ffmpeg("-i", str(path), "-af",
                   f"aresample={SAMPLE_RATE},asetnsamples=n={window},astats=metadata=1:reset=1,"
                   "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
                   "-f", "null", "-")
    # The denoiser turns pauses into digital silence (-inf); clamp them to a floor instead of dropping them
    return [max(float(line.split("=", 1)[1]), NOISE_FLOOR_MIN_DB)
            for line in proc.stdout.splitlines() if "RMS_level=" in line]


def noise_levels(levels: list[float]) -> dict:
    """Noise floor (p10, gaps between words) and speech level (p90) from an RMS window distribution."""
    if not levels:
        return {"noise_db": None, "speech_db": None, "snr_db": None}
    levels = sorted(levels)
    noise, speech = levels[int(len(levels) * 0.10)], levels[int(len(levels) * 0.90)]
    return {"noise_db": round(noise, 1), "speech_db": round(speech, 1), "snr_db": round(speech - noise, 1)}


def measure_noise(path: Path) -> dict:
    """Noise floor and speech level of a file, see noise_levels().

    The estimate is rough for short (< ~15 s) recordings with few pauses.
    """
    return noise_levels(rms_windows(path))


def _measure_chain(path: Path, chain: str, opts: EnhanceOptions) -> dict:
    proc = _ffmpeg("-i", str(path), "-af", f"{chain},{_loudnorm_filter(opts)}", "-f", "null", "-")
    return _parse_loudnorm_json(proc.stderr)


def probe_duration(path: Path) -> float:
    proc = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(path),
    ])
    return float(proc.stdout.strip())


def _output_path(src: Path, opts: EnhanceOptions) -> Path:
    now = datetime.now()
    day_dir = resolve_outputs(opts) / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    base = f"{src.stem}_{opts.preset}_{now.strftime('%H%M%S')}"
    out = day_dir / f"{base}.{opts.fmt}"
    n = 2
    while out.exists():
        out = day_dir / f"{base}-{n}.{opts.fmt}"
        n += 1
    return out


def _denoise(raw: Path, work: Path, opts: EnhanceOptions, log: Callable[[str], None]) -> tuple[Path, str]:
    denoiser = opts.denoiser
    deep_filter = find_deep_filter()
    if denoiser == "auto":
        denoiser = "deepfilter" if deep_filter else "afftdn"
        if not deep_filter:
            log("  ! deep-filter not found, falling back to afftdn (scripts/install_deepfilter.sh)")

    if denoiser == "deepfilter":
        if not deep_filter:
            raise PipelineError("deep-filter not found: run ./scripts/install_deepfilter.sh")
        out_dir = work / "dfn"
        out_dir.mkdir()
        pf = ["--pf"] if opts.postfilter else []
        _run([deep_filter, str(raw), "-o", str(out_dir), "-a", str(opts.atten_lim_db), *pf, "-D"])
        wavs = sorted(out_dir.glob("*.wav"))
        if not wavs:
            raise PipelineError("deep-filter produced no output")
        return wavs[0], f"deepfilternet3 (atten_lim={opts.atten_lim_db}dB{', pf' if opts.postfilter else ''}, -D)"

    if denoiser == "afftdn":
        clean = work / "clean.wav"
        _ffmpeg("-i", str(raw), "-af", AFFTDN_FALLBACK, "-c:a", "pcm_f32le", str(clean))
        return clean, AFFTDN_FALLBACK

    if denoiser == "none":
        return raw, "none"

    raise PipelineError(f"Unknown denoiser: {opts.denoiser}")


def enhance(src: Path, opts: EnhanceOptions | None = None, log: Callable[[str], None] = print,
            out: Path | None = None) -> EnhanceResult:
    opts = opts or EnhanceOptions()
    src = Path(src).expanduser().resolve()
    if not src.is_file():
        raise PipelineError(f"File not found: {src}")
    if opts.preset not in PRESETS:
        raise PipelineError(f"Unknown preset: {opts.preset} (options: {', '.join(PRESETS)})")
    if opts.fmt not in CODECS:
        raise PipelineError(f"Unknown format: {opts.fmt} (options: {', '.join(CODECS)})")
    dsp = PRESETS[opts.preset]
    started = datetime.now()

    with tempfile.TemporaryDirectory(prefix="soundlab-") as tmp:
        work = Path(tmp)

        log("  1/5 Pre-processing (80 Hz HPF, 48 kHz mono)")
        raw = work / "raw.wav"
        _ffmpeg("-i", str(src), "-af", "highpass=f=80:poles=2",
                "-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le", str(raw))
        input_loudness = measure_loudness(raw)
        input_noise = measure_noise(raw)

        log("  2/5 Noise reduction")
        clean, denoise_desc = _denoise(raw, work, opts, log)

        log("  3/5 Loudness measurement (pass 1)")
        m1 = _measure_chain(clean, dsp, opts)
        if float(m1["input_i"]) < -70:
            raise PipelineError("The recording is empty or too quiet (< -70 LUFS)")

        # On quiet recordings the needed gain pushes peaks above the TP target and loudnorm
        # drops from linear to dynamic mode (pumping). So we apply the gain first and keep
        # peaks under the TP target with a limiter.
        # The limiter lowers loudness a little; if loudnorm's make-up gain pushes peaks over
        # again, we lower the ceiling by that much and measure again.
        log("  4/5 Gain + limiter (pass 2)")
        gain_db = opts.target_i - float(m1["input_i"])
        headroom_db = LIMITER_HEADROOM_DB
        for _ in range(LIMITER_MAX_ITER):
            ceiling = 10 ** ((opts.target_tp - headroom_db) / 20)
            chain = f"{dsp},volume={gain_db:.2f}dB,alimiter=limit={ceiling:.4f}:attack=5:release=50:level=false"
            measured = _measure_chain(clean, chain, opts)
            # in linear mode loudnorm applies (target_i - measured_i) of gain
            linear_gain = opts.target_i - float(measured["input_i"])
            overshoot = float(measured["input_tp"]) + linear_gain - (opts.target_tp - TP_MARGIN_DB)
            if overshoot <= 0:
                break
            gain_db += linear_gain
            headroom_db += overshoot + 0.2

        log("  5/5 Linear normalization and output")
        out = out or _output_path(src, opts)
        proc = _ffmpeg("-i", str(clean), "-af", f"{chain},{_loudnorm_filter(opts, measured)}",
                       "-ar", str(SAMPLE_RATE), *CODECS[opts.fmt], str(out))
        final = _parse_loudnorm_json(proc.stderr)

    norm_type = final.get("normalization_type", "unknown")
    if norm_type != "linear":
        log(f"  ! loudnorm fell back to '{norm_type}' mode (target TP/LRA not reachable linearly)")

    data = {
        "source": str(src),
        "output": str(out),
        "created_at": started.isoformat(timespec="seconds"),
        "elapsed_sec": round((datetime.now() - started).total_seconds(), 2),
        "duration_sec": round(probe_duration(out), 2),
        "options": {k: (str(v) if isinstance(v, Path) else v) for k, v in asdict(opts).items()},
        "denoiser": denoise_desc,
        "dsp_chain": chain,
        "pre_gain_db": round(gain_db, 2),
        "normalization_type": norm_type,
        "loudness_input": input_loudness,
        "loudness_output": measure_loudness(out),
        "noise_input": input_noise,
        "noise_output": measure_noise(out),
    }
    report = out.with_suffix(".json")
    report.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return EnhanceResult(output=out, report=report, data=data)
