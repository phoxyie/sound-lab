"""Voice enhancement pipeline.

Flow: 80 Hz HPF + 48 kHz mono WAV → denoise (DeepFilterNet3 / Resemble Enhance / Sidon / afftdn)
→ DSP chain → 2-pass linear loudnorm → outputs/YYYY-MM-DD/
"""

from __future__ import annotations

import json
import math
import os
import queue
import shutil
import subprocess
import tempfile
import threading
from dataclasses import asdict, dataclass, field
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
PROGRESS_POLL_SEC = 0.25  # how often a slow external step reports progress (and so notices a cancel)

CODECS = {
    "m4a": ["-c:a", "aac", "-b:a", "192k"],
    "wav": ["-c:a", "pcm_s24le"],
}
# Video outputs (timeline export / Tighten / extract of a video): always MP4, not a user-selectable format
VIDEO_CODEC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart"]


DENOISERS = ["auto", "deepfilter", "resemble", "sidon", "afftdn", "none"]
GENERATIVE = {"resemble", "sidon"}  # rebuild the voice; tagged in output names, run as external workers
RESEMBLE_VENV = PROJECT_ROOT / "tools" / "resemble-venv"
RESEMBLE_MODEL = PROJECT_ROOT / "tools" / "resemble-model" / "enhancer_stage2"
RESEMBLE_WORKER = Path(__file__).resolve().parent / "resemble_worker.py"
RESEMBLE_SOLVERS = ["midpoint", "rk4", "euler"]
RESEMBLE_NFE_MAX = 128
RESEMBLE_SEED_MAX = 9999
# Seconds of work per second of audio = A + B × nfe — measured on the M1 MacBook Air, CPU, a 34 s take
# (nfe 16: 2.97×, 32: 3.57×, 64: 4.44×; the 8.4 s take fits too). Long runs may be slower once the
# fanless Mac throttles, so the estimate is a lower bound; clips of a few seconds take ~5 s more
# (model load) — they stay far under the GUI's confirm threshold anyway.
RESEMBLE_RATE_A, RESEMBLE_RATE_B = 2.52, 0.0305
SIDON_VENV = PROJECT_ROOT / "tools" / "sidon-venv"
SIDON_MODEL = PROJECT_ROOT / "tools" / "sidon-model"
SIDON_WORKER = Path(__file__).resolve().parent / "sidon_worker.py"
SIDON_FILES = ["feature_extractor_cpu.pt", "decoder_cpu.pt", "preprocessor_config.json"]


@dataclass
class ResembleSettings:
    """Resemble Enhance (full mode: denoise + generative voice rebuild).

    Defaults from an ear test on a real take (2026-10-29): tau 0.5 sounded better than 0.2;
    nfe 32 vs 64 and lambd 0.5 vs 1.0 were inaudible (nfe 32 is ~25% faster).
    """
    tau: float = 0.5  # prior temperature 0–1: lower stays closer to the input, higher varies more
    nfe: int = 32  # solver function evaluations 1–128: fewer = faster
    lambd: float = 1.0  # denoise strength 0–1 before the rebuild
    solver: str = "midpoint"  # midpoint | rk4 | euler
    # The rebuild starts from random noise; each seed is a slightly different take (as much as ±3 dB
    # in the 6–12 kHz band and the pauses on the test take). Same settings + seed = same output.
    seed: int = 0

    def validate(self) -> None:
        if not 0 <= self.tau <= 1:
            raise PipelineError("Resemble temperature (tau) must be between 0 and 1")
        if not 0 <= self.lambd <= 1:
            raise PipelineError("Resemble strength (lambd) must be between 0 and 1")
        if not (1 <= self.nfe <= RESEMBLE_NFE_MAX and float(self.nfe).is_integer()):
            raise PipelineError(f"Resemble steps (nfe) must be a whole number from 1 to {RESEMBLE_NFE_MAX}")
        if self.solver not in RESEMBLE_SOLVERS:
            raise PipelineError(f"Unknown Resemble solver: {self.solver} (options: {', '.join(RESEMBLE_SOLVERS)})")
        if not (0 <= self.seed <= RESEMBLE_SEED_MAX and float(self.seed).is_integer()):
            raise PipelineError(f"Resemble variation (seed) must be a whole number from 0 to {RESEMBLE_SEED_MAX}")


@dataclass
class EnhanceOptions:
    preset: str = "flat"  # ear test: full DeepFilterNet without EQ sounded best
    denoiser: str = "auto"  # auto | deepfilter | resemble | sidon | afftdn | none
    atten_lim_db: float = 30.0
    postfilter: bool = True
    resemble: ResembleSettings = field(default_factory=ResembleSettings)
    target_i: float = -16.0
    target_tp: float = -1.5
    target_lra: float = 11.0
    min_lufs: float = -70.0  # reject recordings quieter than this as empty/silent
    fmt: str = "m4a"
    outputs_root: Path | None = None  # None → the current project's outputs/ folder


@dataclass
class EnhanceResult:
    output: Path
    report: Path
    data: dict


class PipelineError(RuntimeError):
    pass


class Cancelled(PipelineError):
    """Raised from a progress callback to stop a running enhancement."""


# Progress callback: overall fraction 0–1. It may raise Cancelled to stop the job.
Progress = Callable[[float], None]


def resolve_outputs(opts: EnhanceOptions) -> Path:
    return opts.outputs_root or projects.current().outputs


def _venv_python(venv: Path) -> Path:
    win = venv / "Scripts" / "python.exe"
    return win if os.name == "nt" and win.is_file() else venv / "bin" / "python"


def find_deep_filter() -> str | None:
    exe = "deep-filter.exe" if os.name == "nt" else "deep-filter"
    local = PROJECT_ROOT / "tools" / exe
    if local.is_file():
        return str(local)
    local_nobin = PROJECT_ROOT / "tools" / "deep-filter"
    if local_nobin.is_file():
        return str(local_nobin)
    return shutil.which("deep-filter")


def find_resemble() -> Path | None:
    """The Python of Resemble Enhance's own venv, if it and the model are installed."""
    python = _venv_python(RESEMBLE_VENV)
    weights = RESEMBLE_MODEL / "ds" / "G" / "default" / "mp_rank_00_model_states.pt"
    return python if python.is_file() and weights.is_file() else None


def estimate_resemble_seconds(duration_sec: float, nfe: int) -> float:
    """Rough processing time of Resemble Enhance for this much audio (see RESEMBLE_RATE_A/B)."""
    return duration_sec * (RESEMBLE_RATE_A + RESEMBLE_RATE_B * nfe)


def find_sidon() -> Path | None:
    """The Python of Sidon's own venv, if it and the model are installed."""
    python = _venv_python(SIDON_VENV)
    return python if python.is_file() and all((SIDON_MODEL / f).is_file() for f in SIDON_FILES) else None


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-30:])
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
    valid_measured = False
    if measured:
        try:
            m_i = float(measured.get("input_i", "nan"))
            m_tp = float(measured.get("input_tp", "nan"))
            m_lra = float(measured.get("input_lra", "nan"))
            m_thresh = float(measured.get("input_thresh", "nan"))
            m_offset = float(measured.get("target_offset", "nan"))
            if (not any(math.isinf(x) or math.isnan(x) for x in (m_i, m_tp, m_lra, m_thresh, m_offset))
                    and -99.0 <= m_i <= 0.0
                    and -99.0 <= m_offset <= 99.0):
                valid_measured = True
                lra = min(max(lra, m_lra + 0.1), 50.0)
        except (ValueError, TypeError):
            valid_measured = False

    f = f"loudnorm=I={opts.target_i}:TP={opts.target_tp}:LRA={lra:g}:print_format=json"
    if valid_measured:
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
    try:
        lufs = float(m["input_i"])
        if math.isinf(lufs) or math.isnan(lufs) or lufs < -90.0:
            lufs = -99.0
    except (ValueError, TypeError):
        lufs = -99.0
    try:
        tp = float(m["input_tp"])
        if math.isinf(tp) or math.isnan(tp):
            tp = -99.0
    except (ValueError, TypeError):
        tp = -99.0
    try:
        lra = float(m["input_lra"])
        if math.isinf(lra) or math.isnan(lra):
            lra = 0.0
    except (ValueError, TypeError):
        lra = 0.0
    return {"lufs": lufs, "true_peak": tp, "lra": lra}


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
    tag = f"{opts.preset}_{opts.denoiser}" if opts.denoiser in GENERATIVE else opts.preset
    base = f"{src.stem}_{tag}_{now.strftime('%H%M%S')}"
    out = day_dir / f"{base}.{opts.fmt}"
    n = 2
    while out.exists():
        out = day_dir / f"{base}-{n}.{opts.fmt}"
        n += 1
    return out


def _run_worker(name: str, cmd: list[str], out: Path, log_file: Path, progress: Progress) -> Path:
    """Runs a model worker (its own venv's Python) that prints {"progress": 0..1} JSON lines.

    No network: Hugging Face libraries in the workers run offline, everything is loaded from tools/.
    """
    env = {**os.environ, "TQDM_DISABLE": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    with open(log_file, "w") as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True, env=env)
        # A progress line can take ~20 s (Resemble: model load + the first chunk's denoise pass), so the
        # lines are read in a thread and progress is reported every PROGRESS_POLL_SEC regardless:
        # a cancel raised from the callback then stops the worker right away.
        lines: queue.Queue[str | None] = queue.Queue()

        def read() -> None:
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=read, daemon=True).start()
        last = 0.0
        try:
            while True:
                try:
                    line = lines.get(timeout=PROGRESS_POLL_SEC)
                except queue.Empty:
                    progress(last)
                    continue
                if line is None:
                    break
                try:
                    last = float(json.loads(line)["progress"])
                except (ValueError, KeyError, TypeError):
                    continue
                progress(last)
            proc.wait()
        finally:
            if proc.poll() is None:  # cancelled (or failed) mid-run: don't leave it burning the CPU
                proc.kill()
                proc.wait()
    if proc.returncode != 0 or not out.is_file():
        tail = "\n".join(log_file.read_text(errors="replace").strip().splitlines()[-10:])
        raise PipelineError(f"{name} failed (exit {proc.returncode})\n{tail}")
    return out


def ffmpeg_progress(args: list[str], duration: float, progress: Progress | None) -> None:
    """Runs ffmpeg with progress reports (fraction of `duration` written) so a long render (a video
    encode) can show progress and be cancelled: a Cancelled raised by the callback kills ffmpeg."""
    if progress is None:
        _ffmpeg(*args)
        return
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats", *args]
    with tempfile.TemporaryFile(mode="w+") as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True)
        lines: queue.Queue[str | None] = queue.Queue()

        def read() -> None:
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=read, daemon=True).start()
        last = 0.0
        try:
            while True:
                try:
                    line = lines.get(timeout=PROGRESS_POLL_SEC)
                except queue.Empty:
                    progress(last)
                    continue
                if line is None:
                    break
                key, _, value = line.strip().partition("=")
                if key == "out_time_us" and duration > 0:
                    try:
                        last = min(1.0, max(0.0, int(value) / 1e6 / duration))
                    except ValueError:
                        continue
                    progress(last)
            proc.wait()
        finally:
            if proc.poll() is None:  # cancelled mid-run: don't leave it burning the CPU
                proc.kill()
                proc.wait()
            proc.stdout.close()
        if proc.returncode != 0:
            err.seek(0)
            tail = "\n".join(err.read().strip().splitlines()[-30:])
            raise PipelineError(f"Command failed: ffmpeg\n{tail}")


def _resemble(raw: Path, work: Path, rs: ResembleSettings, progress: Progress) -> Path:
    python = find_resemble()
    if not python:
        raise PipelineError("Resemble Enhance isn't installed: run ./scripts/install_resemble.sh")
    out = work / "resemble.wav"
    cmd = [str(python), "-I", str(RESEMBLE_WORKER), str(raw), str(out), str(RESEMBLE_MODEL),
           str(rs.tau), str(int(rs.nfe)), str(rs.lambd), rs.solver, str(int(rs.seed))]
    return _run_worker("Resemble Enhance", cmd, out, work / "resemble.log", progress)


def _sidon(raw: Path, work: Path, progress: Progress) -> Path:
    python = find_sidon()
    if not python:
        raise PipelineError("Sidon isn't installed: run ./scripts/install_sidon.sh")
    out = work / "sidon.wav"
    cmd = [str(python), "-I", str(SIDON_WORKER), str(raw), str(out), str(SIDON_MODEL)]
    return _run_worker("Sidon", cmd, out, work / "sidon.log", progress)


def _denoise(raw: Path, work: Path, opts: EnhanceOptions, log: Callable[[str], None],
             progress: Progress) -> tuple[Path, str]:
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

    if denoiser == "resemble":
        rs = opts.resemble
        clean = _resemble(raw, work, rs, progress)
        return clean, (f"resemble-enhance (tau={rs.tau:g}, nfe={int(rs.nfe)}, lambd={rs.lambd:g}, "
                       f"solver={rs.solver}, seed={int(rs.seed)}, cpu)")

    if denoiser == "sidon":
        return _sidon(raw, work, progress), "sidon v0.1 (w2v-BERT 2.0 + DAC decoder, cpu)"

    if denoiser == "afftdn":
        clean = work / "clean.wav"
        _ffmpeg("-i", str(raw), "-af", AFFTDN_FALLBACK, "-c:a", "pcm_f32le", str(clean))
        return clean, AFFTDN_FALLBACK

    if denoiser == "none":
        return raw, "none"

    raise PipelineError(f"Unknown denoiser: {opts.denoiser}")


# Where each of the 5 stages starts in the overall progress; Resemble's denoise takes nearly all of it,
# Sidon's most of it (it runs at ~0.45× the recording's length)
STAGES = {"resemble": [0.0, 0.02, 0.92, 0.95, 0.98], "sidon": [0.0, 0.05, 0.8, 0.88, 0.95],
          "other": [0.0, 0.15, 0.45, 0.65, 0.9]}


def enhance(src: Path, opts: EnhanceOptions | None = None, log: Callable[[str], None] = print,
            out: Path | None = None, progress: Progress | None = None) -> EnhanceResult:
    opts = opts or EnhanceOptions()
    src = Path(src).expanduser().resolve()
    if not src.is_file():
        raise PipelineError(f"File not found: {src}")
    if opts.preset not in PRESETS:
        raise PipelineError(f"Unknown preset: {opts.preset} (options: {', '.join(PRESETS)})")
    if opts.fmt not in CODECS:
        raise PipelineError(f"Unknown format: {opts.fmt} (options: {', '.join(CODECS)})")
    if opts.denoiser not in DENOISERS:
        raise PipelineError(f"Unknown denoiser: {opts.denoiser} (options: {', '.join(DENOISERS)})")
    if opts.denoiser == "resemble":
        opts.resemble.validate()
    dsp = PRESETS[opts.preset]
    started = datetime.now()
    starts = STAGES.get(opts.denoiser, STAGES["other"])
    report = progress or (lambda _: None)

    def stage(i: int, msg: str) -> None:
        log(msg)
        report(starts[i])

    def denoise_progress(f: float) -> None:  # the denoiser's own 0–1, mapped into its stage
        report(starts[1] + (starts[2] - starts[1]) * f)

    with tempfile.TemporaryDirectory(prefix="soundlab-") as tmp:
        work = Path(tmp)

        stage(0, "  1/5 Pre-processing (80 Hz HPF, 48 kHz mono)")
        raw = work / "raw.wav"
        _ffmpeg("-i", str(src), "-af", "highpass=f=80:poles=2",
                "-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le", str(raw))
        input_loudness = measure_loudness(raw)
        input_noise = measure_noise(raw)

        stage(1, "  2/5 Noise reduction")
        clean, denoise_desc = _denoise(raw, work, opts, log, denoise_progress)

        stage(2, "  3/5 Loudness measurement (pass 1)")
        m1 = _measure_chain(clean, dsp, opts)
        lufs_val = float(m1.get("input_i", "-inf"))
        if math.isinf(lufs_val) or math.isnan(lufs_val) or lufs_val < opts.min_lufs:
            raise PipelineError(f"The recording is empty or too quiet (< {opts.min_lufs:.0f} LUFS)")

        # On quiet recordings the needed gain pushes peaks above the TP target and loudnorm
        # drops from linear to dynamic mode (pumping). So we apply the gain first and keep
        # peaks under the TP target with a limiter.
        # The limiter lowers loudness a little; if loudnorm's make-up gain pushes peaks over
        # again, we lower the ceiling by that much and measure again.
        stage(3, "  4/5 Gain + limiter (pass 2)")
        gain_db = opts.target_i - lufs_val
        headroom_db = LIMITER_HEADROOM_DB
        measured = None
        for _ in range(LIMITER_MAX_ITER):
            ceiling = 10 ** ((opts.target_tp - headroom_db) / 20)
            chain = f"{dsp},volume={gain_db:.2f}dB,alimiter=limit={ceiling:.4f}:attack=5:release=50:level=false"
            measured = _measure_chain(clean, chain, opts)
            m_i = float(measured.get("input_i", "-inf"))
            m_tp = float(measured.get("input_tp", "-inf"))
            if math.isinf(m_i) or math.isnan(m_i) or math.isinf(m_tp) or math.isnan(m_tp):
                break
            # in linear mode loudnorm applies (target_i - measured_i) of gain
            linear_gain = opts.target_i - m_i
            overshoot = m_tp + linear_gain - (opts.target_tp - TP_MARGIN_DB)
            if overshoot <= 0:
                break
            gain_db += linear_gain
            headroom_db += overshoot + 0.2

        stage(4, "  5/5 Linear normalization and output")
        out = out or _output_path(src, opts)
        out_fmt = out.suffix.lstrip(".").lower()
        codec_args = CODECS.get(out_fmt, CODECS[opts.fmt])
        proc = _ffmpeg("-i", str(clean), "-af", f"{chain},{_loudnorm_filter(opts, measured)}",
                       "-ar", str(SAMPLE_RATE), *codec_args, str(out))
        final = _parse_loudnorm_json(proc.stderr)
    report(1.0)

    norm_type = final.get("normalization_type", "unknown")
    if norm_type != "linear":
        log(f"  ! loudnorm fell back to '{norm_type}' mode (target TP/LRA not reachable linearly)")

    options = {k: (str(v) if isinstance(v, Path) else v) for k, v in asdict(opts).items()}
    if opts.denoiser != "resemble":
        options.pop("resemble")
    data = {
        "source": str(src),
        "output": str(out),
        "created_at": started.isoformat(timespec="seconds"),
        "elapsed_sec": round((datetime.now() - started).total_seconds(), 2),
        "duration_sec": round(probe_duration(out), 2),
        "options": options,
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
