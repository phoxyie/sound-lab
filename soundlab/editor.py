"""Timeline render: trim clips (atrim), speed them up (atempo), join them (concat)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Callable

from .pipeline import (CODECS, GENERATIVE, RMS_WINDOW, SAMPLE_RATE, VIDEO_CODEC, EnhanceOptions, PipelineError, Progress,
                       _ffmpeg, enhance, ffmpeg_progress, noise_levels, probe_duration, resolve_outputs, rms_windows)

SPEED_MIN, SPEED_MAX = 0.5, 2.0
# Micro-fade at every clip edge: a cut mid-waveform jumps to/from a non-zero sample and clicks
EDGE_FADE = 0.010
PAUSE_MAX = 0.05     # Tighten's slider full left ("Skip silence", social-media jump cuts): every gap
                     # longer than ~0.1 s goes down to this, half kept on each side
BLIP_MAX = 0.25      # a sound this short inside a pause that stays SPEECH_DROP under the speech level
                     # (breath, lip smack, click) is part of the pause, not speech
CUT_FADE = 0.005     # fade at each side of every Tighten cut
PAUSE_SLIDER_MAX = 1.0  # the editor slider's right end (seconds)
SKIP_DROP = 18.0     # full left (Skip silence): everything this many dB under the voice is silence
PAUSE_MIN_SNR = 12.0  # below this, pauses can't be told apart from speech reliably
# A pause is only shortened if it's this much longer than the limit (a few 20 ms windows): a kept
# 0.4 s pause can measure a window or two longer after the cut, and tightening again shouldn't nibble
PAUSE_SLACK = 0.05
SPEECH_MIN = 0.06     # shorter bursts (a mouse / keyboard click) don't count as speech
SPEECH_DROP = 15.0    # nor do sounds that never get within this many dB of the speech level (a trackpad
                      # click with its room decay can last ~80 ms, but sits ~20 dB under the voice)
TRIM_PAD = (0.10, 0.15)  # kept before the first / after the last speech: lead-in and decay


@dataclass
class Clip:
    path: Path
    start: float
    end: float
    speed: float = 1.0

    def validate(self) -> None:
        if not Path(self.path).is_file():
            raise PipelineError(f"File not found: {self.path}")
        if not (SPEED_MIN <= self.speed <= SPEED_MAX):
            raise PipelineError(f"Speed must be between {SPEED_MIN} and {SPEED_MAX}: {self.speed}")
        if not (0 <= self.start < self.end):
            raise PipelineError(f"Invalid trim range: {self.start}–{self.end}")


def merge_continuous(clips: list[Clip]) -> list[Clip]:
    """Joins neighbours that continue each other in the same file at the same speed (a split clip).

    Rendered separately, the resampler and atempo would restart at the cut (a seam / click);
    merged, a split clip renders exactly like the whole one.
    """
    merged: list[Clip] = []
    for c in clips:
        prev = merged[-1] if merged else None
        if (prev and Path(prev.path) == Path(c.path) and abs(prev.speed - c.speed) < 1e-6
                and abs(prev.end - c.start) < 1e-6):
            merged[-1] = replace(prev, end=c.end)
        else:
            merged.append(c)
    return merged


def render(clips: list[Clip], out: Path, codec_args: list[str]) -> None:
    """Joins the clips in order into one 48 kHz mono file; sources are left untouched."""
    if not clips:
        raise PipelineError("The timeline is empty")
    for c in clips:
        c.validate()
    clips = merge_continuous(clips)
    inputs, parts = [], []
    for i, c in enumerate(clips):
        inputs += ["-i", str(c.path)]
        chain = (f"[{i}:a]atrim=start={c.start:.3f}:end={c.end:.3f},asetpts=PTS-STARTPTS,"
                 f"aresample={SAMPLE_RATE},aformat=sample_fmts=fltp:channel_layouts=mono")
        if abs(c.speed - 1.0) > 1e-3:
            chain += f",atempo={c.speed:.3f}"  # speed change that keeps the pitch
        # atempo's output length differs from (end - start) / speed by up to ~20 ms, so the fade-out is
        # anchored to the real end: reverse, fade in, reverse back
        fade = min(EDGE_FADE, (c.end - c.start) / c.speed / 2)
        chain += f",afade=t=in:d={fade:.4f},areverse,afade=t=in:d={fade:.4f},areverse"
        parts.append(f"{chain}[a{i}]")
    joined = "".join(f"[a{i}]" for i in range(len(clips)))
    parts.append(f"{joined}concat=n={len(clips)}:v=0:a=1[out]")
    _ffmpeg(*inputs, "-filter_complex", ";".join(parts), "-map", "[out]", *codec_args, str(out))


def silence_threshold(levels: list[float]) -> float | None:
    """RMS level (dBFS) under which a 20 ms window counts as silence; None if speech and noise are too
    close to tell apart. Sits between this recording's noise floor and speech level."""
    noise = noise_levels(levels)
    if noise["snr_db"] is None or noise["snr_db"] < PAUSE_MIN_SNR:
        return None
    # very clean audio (digital silence in pauses) would put it near -100 dB, where faint word tails
    # still count as speech: stay within 40 dB of the speech level
    return max(noise["noise_db"] + max(6.0, 0.3 * noise["snr_db"]), noise["speech_db"] - 40.0)


def speech_bounds(path: Path, threshold: float | None = None, speech_db: float | None = None,
                  pad: tuple[float, float] = TRIM_PAD) -> tuple[float, float]:
    """Start / end of the speech in a file, with a short lead-in and decay (for trimming silence)."""
    levels = rms_windows(path)
    threshold = threshold if threshold is not None else silence_threshold(levels)
    if threshold is None:
        raise PipelineError("Can't tell speech from background noise in this recording")
    need = round(SPEECH_MIN / RMS_WINDOW)
    floor = (speech_db if speech_db is not None else noise_levels(levels)["speech_db"]) - SPEECH_DROP
    runs, start = [], None  # (start, end) window indexes of sounds that count as speech
    for i, level in enumerate([*levels, -999.0]):  # the sentinel closes a sound at the end
        if level >= threshold:
            start = i if start is None else start
            continue
        if start is not None and i - start >= need and max(levels[start:i]) >= floor:
            runs.append((start, i))
        start = None
    if not runs:
        raise PipelineError("No speech found in this recording")
    total = len(levels) * RMS_WINDOW
    first, last = runs[0][0] * RMS_WINDOW, runs[-1][1] * RMS_WINDOW
    return round(max(0.0, first - pad[0]), 3), round(min(total, last + pad[1]), 3)


def _silent_windows(levels: list[float], threshold: float, speech_db: float | None = None) -> list[bool]:
    """Which 20 ms windows belong to a pause: under the threshold, plus short quiet sounds between
    them (a breath or lip smack never reaches the voice's level, so it doesn't split a pause)."""
    silent = [lv < threshold for lv in levels]
    floor = (speech_db if speech_db is not None else noise_levels(levels)["speech_db"]) - SPEECH_DROP
    blip = round(BLIP_MAX / RMS_WINDOW)
    i = 0
    while i < len(silent):
        if silent[i]:
            i += 1
            continue
        j = i
        while j < len(silent) and not silent[j]:
            j += 1
        if 0 < i and j < len(silent) and j - i <= blip and max(levels[i:j]) < floor:
            silent[i:j] = [True] * (j - i)
        i = j
    return silent


def pause_cuts(path: Path, max_pause: float = PAUSE_MAX, threshold: float | None = None,
               speech_db: float | None = None) -> tuple[list[list[float]], int]:
    """Segments to keep so that no pause is longer than max_pause, and how many pauses were shortened.

    A pause is a run of 20 ms windows quieter than a threshold between the noise floor and the speech
    level of this recording (so it adapts to quiet / noisy takes), short quiet sounds inside it
    included. Half of max_pause is kept on each side, so word endings and onsets aren't touched.
    """
    levels = rms_windows(path)
    threshold = threshold if threshold is not None else silence_threshold(levels)
    if threshold is None:
        return [], 0
    silent = _silent_windows(levels, threshold, speech_db)
    total = len(levels) * RMS_WINDOW
    keep, pos, shortened, run_start = [], 0.0, 0, None
    for i, quiet in enumerate([*silent, False]):  # the sentinel closes a pause at the end
        if quiet:
            run_start = i if run_start is None else run_start
            continue
        if run_start is not None and (i - run_start) * RMS_WINDOW > max_pause + PAUSE_SLACK:
            cut_from = run_start * RMS_WINDOW + max_pause / 2
            cut_to = min(total, i * RMS_WINDOW - max_pause / 2)
            keep.append([round(pos, 3), round(cut_from, 3)])
            pos = cut_to
            shortened += 1
        run_start = None
    if not shortened:
        return [], 0
    keep.append([round(pos, 3), round(total, 3)])
    return keep, shortened


def has_video_stream(path: Path) -> bool:
    """Checks whether the file has a video stream that is not an attached album art image."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_type:disposition=attached_pic",
             "-of", "json", str(path)],
            capture_output=True, text=True, check=True
        )
        streams = json.loads(proc.stdout).get("streams", [])
        return any(s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic") for s in streams)
    except Exception:
        return False


def has_audio_stream(path: Path) -> bool:
    """Checks whether the file has at least one audio stream."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_type",
             "-of", "json", str(path)],
            capture_output=True, text=True, check=True
        )
        return bool(json.loads(proc.stdout).get("streams"))
    except Exception:
        return False


def probe_video_info(path: Path) -> dict:
    """Video width, height (as displayed: a phone's rotation applied, rounded down to even numbers for
    H.264) and frame rate (a fraction string such as "30000/1001")."""
    info = {"width": 1920, "height": 1080, "fps": "30/1"}
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate:stream_side_data=rotation"
             ":stream_tags=rotate", "-of", "json", str(path)],
            capture_output=True, text=True, check=True
        )
        streams = json.loads(proc.stdout).get("streams", [])
    except (OSError, subprocess.CalledProcessError, ValueError):
        return info
    if not streams:
        return info
    s = streams[0]
    w, h = int(s.get("width") or 1920), int(s.get("height") or 1080)
    rotation = s.get("tags", {}).get("rotate") or next(
        (d["rotation"] for d in s.get("side_data_list", []) if "rotation" in d), 0)
    try:
        if int(float(rotation)) % 180 == 90 or int(float(rotation)) % 180 == -90:
            w, h = h, w
    except ValueError:
        pass
    fps = "30/1"
    for key in ("avg_frame_rate", "r_frame_rate"):  # avg: phones record variable frame rate
        try:
            f = Fraction(s.get(key) or "0/1")
        except (ValueError, ZeroDivisionError):
            continue
        if 1 <= f <= 240:
            fps = f"{f.numerator}/{f.denominator}"
            break
    return {"width": max(2, w - w % 2), "height": max(2, h - h % 2), "fps": fps}


KEEP_CHUNK = 40  # Tighten: segments per ffmpeg run (Windows' 32k-character command line, open inputs)


def keep_segments(src: Path, keep: list[list[float]], out: Path, codec_args: list[str]) -> None:
    """Writes only the given [start, end] segments of src, back to back, with a short fade on each
    side of every cut so none of them clicks (src is a WAV: -ss seeks are sample-accurate).

    Batches segments into chunks to stay well within OS command-line length limits (e.g. Windows
    32k character limit) and process descriptor limits on long recordings.
    """
    if not keep:
        return
    with tempfile.TemporaryDirectory(prefix="soundlab-keep-") as tmp:
        tmp_dir = Path(tmp)

        def _render_batch(segments: list[list[float]], dest: Path) -> None:
            inputs, parts = [], []
            for i, (a, b) in enumerate(segments):
                inputs += ["-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(src)]
                fade = min(CUT_FADE, (b - a) / 2)
                parts.append(f"[{i}:a]afade=t=in:d={fade:.4f},areverse,afade=t=in:d={fade:.4f},areverse[s{i}]")
            parts.append("".join(f"[s{i}]" for i in range(len(segments))) + f"concat=n={len(segments)}:v=0:a=1[out]")
            _ffmpeg(*inputs, "-filter_complex", ";".join(parts), "-map", "[out]", *codec_args, str(dest))

        if len(keep) <= KEEP_CHUNK:
            _render_batch(keep, out)
            return

        chunk_files = []
        for idx, i in enumerate(range(0, len(keep), KEEP_CHUNK)):
            batch = keep[i : i + KEEP_CHUNK]
            c_file = tmp_dir / f"chunk_{idx}.wav"
            _render_batch(batch, c_file)
            chunk_files.append(c_file)

        list_file = tmp_dir / "concat_list.txt"
        list_file.write_text("\n".join(f"file '{c.resolve().as_posix()}'" for c in chunk_files), encoding="utf-8")
        _ffmpeg("-f", "concat", "-safe", "0", "-i", str(list_file), *codec_args, str(out))


def _balance_expr(items: list[str]) -> str:
    """Combines a list of boolean expressions into a balanced binary tree to avoid FFmpeg recursion limits."""
    if not items:
        return "0"
    if len(items) == 1:
        return items[0]
    mid = len(items) // 2
    return f"({_balance_expr(items[:mid])}+{_balance_expr(items[mid:])})"


def _frame_counts(durations: list[float], fps: Fraction) -> list[int]:
    """Frames per segment so every segment boundary lands within half a frame of the audio's: rounding
    each segment on its own would let the picture drift from the sound by up to a frame per cut."""
    counts, total, prev = [], 0.0, 0
    for d in durations:
        total += d
        end = round(total * fps)
        counts.append(end - prev)
        prev = end
    return counts


def keep_video_segments(src: Path, segments: list[tuple[float, float]], tight_audio: Path, out: Path) -> None:
    """Writes the given (source start, duration) segments of a video src back to back in one pass,
    muxed with tight_audio (the same segments' audio, already cut and faded).

    The picture is resampled to a constant frame rate (fps=…:start_time=0, so frame n shows source time
    n / fps) and whole frames are picked per segment: segment k gets as many frames as its audio
    needs (_frame_counts), so the cuts stay in sync however many there are.
    """
    if not segments:
        return
    fps = Fraction(probe_video_info(src)["fps"])
    counts = _frame_counts([d for _, d in segments], fps)
    conds, next_free = [], 0
    for (a, _), n in zip(segments, counts):
        first = max(round(a * fps), next_free)  # segments never share a frame
        if n > 0:
            conds.append(f"between(n,{first},{first + n - 1})")
        next_free = first + n
    with tempfile.TemporaryDirectory(prefix="soundlab-vidcut-") as tmp:
        filter_str = (f"[0:v]fps=fps={fps}:start_time=0,select='{_balance_expr(conds)}',"
                      f"setpts=N/({fps}*TB)[outv]")
        script_file = Path(tmp) / "video_filter.txt"
        script_file.write_text(filter_str, encoding="utf-8")
        _ffmpeg("-i", str(src), "-i", str(tight_audio),
                "-/filter_complex", str(script_file),
                "-map", "[outv]", "-map", "1:a", *VIDEO_CODEC, str(out))


def pause_threshold(threshold: float, speech_db: float, max_pause: float) -> float:
    """Slider position → silence threshold. Full left (Skip silence) treats everything SKIP_DROP dB
    under the voice as silence — room tone, breaths, word tails — like Voice Memos; towards the right
    end it relaxes to the recording's own noise-based threshold, where only real silence counts."""
    t = min(1.0, max(0.0, (max_pause - PAUSE_MAX) / (PAUSE_SLIDER_MAX - PAUSE_MAX)))
    return max(threshold, speech_db - (SKIP_DROP + t * (40.0 - SKIP_DROP)))


def tighten(src: Path, start: float, end: float, out_dir: Path, max_pause: float = PAUSE_MAX) -> dict | None:
    """A tightened copy of src[start:end] → <name>_tight.wav (or .mp4) (+ .json report) in out_dir.

    Silence at both ends is trimmed to a short lead-in / decay (speech_bounds) and inner pauses longer
    than max_pause are cut down to it (pause_cuts). The source is left untouched; None if there is
    nothing to shorten.
    """
    with tempfile.TemporaryDirectory(prefix="soundlab-tight-") as tmp:
        part = Path(tmp) / "part.wav"
        render([Clip(src, start, end, 1.0)], part, ["-c:a", "pcm_f32le"])
        total = probe_duration(part)
        # A file that was tightened before keeps the levels it was measured with: with less silence
        # left, fresh estimates rise and each pass would cut a little more (breaths, quiet endings)
        try:
            prev = json.loads(src.with_suffix(".json").read_text())
        except (OSError, ValueError):
            prev = {}
        threshold, speech_db = prev.get("threshold_db"), prev.get("speech_db")
        if threshold is None or speech_db is None:
            levels = rms_windows(part)
            threshold, speech_db = silence_threshold(levels), noise_levels(levels)["speech_db"]
        cut_at = pause_threshold(threshold, speech_db, max_pause)
        # the tighter the setting, the sooner the first word starts (and the last one ends)
        pad = (min(TRIM_PAD[0], max(0.05, max_pause)), min(TRIM_PAD[1], max(0.08, max_pause)))
        head, tail = speech_bounds(part, cut_at, speech_db, pad)
        keep, _ = pause_cuts(part, max_pause, cut_at, speech_db)
        keep = [[max(a, head), min(b, tail)] for a, b in (keep or [[0.0, total]])
                if min(b, tail) - max(a, head) > 0.01]
        removed = total - sum(b - a for a, b in keep)
        if removed < 0.05:
            return None
        is_video = has_video_stream(src)
        ext = ".mp4" if is_video else ".wav"
        base = src.stem.removesuffix("_tight")
        out_dir.mkdir(parents=True, exist_ok=True)
        out, n = out_dir / f"{base}_tight{ext}", 2
        while out.exists():
            out, n = out_dir / f"{base}_tight-{n}{ext}", n + 1
        if is_video:
            tight_audio = Path(tmp) / "tight_audio.wav"
            keep_segments(part, keep, tight_audio, ["-c:a", "pcm_s24le"])
            # the same rounding keep_segments' -ss / -t use, so picture and sound cut at the same points
            segments = [(float(f"{start:.3f}") + float(f"{a:.3f}"), float(f"{b - a:.3f}")) for a, b in keep]
            keep_video_segments(src, segments, tight_audio, out)
        else:
            keep_segments(part, keep, out, ["-c:a", "pcm_s24le"])
    data = {
        "source": str(src), "output": str(out), "created_at": datetime.now().isoformat(timespec="seconds"),
        "tightened": True, "range": [round(start, 3), round(end, 3)], "max_pause_sec": max_pause,
        "threshold_db": round(threshold, 2), "speech_db": speech_db, "cut_below_db": round(cut_at, 2),
        "trimmed_sec": [round(max(0.0, keep[0][0]), 2), round(max(0.0, total - keep[-1][1]), 2)],
        "pauses_shortened": len(keep) - 1, "removed_sec": round(removed, 2),
        "duration_sec": round(probe_duration(out), 2), "keep": keep, "is_video": is_video,
    }
    out.with_suffix(".json").write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return data


def extract(src: Path, start: float, end: float, out_dir: Path) -> dict:
    """src[start:end] as a file of its own → <name>_part.wav (or .mp4) (+ .json report) in out_dir.

    The editor's "copy the selection to a new track": the new track is a recording of its own, not
    a view into the source. The source is left untouched. Levels measured for Tighten carry over, so
    tightening a part of a tightened file cuts no more than tightening the whole would.
    """
    if end - start < 0.05:
        raise PipelineError("The selection is too short")
    is_video = has_video_stream(src)
    ext = ".mp4" if is_video else ".wav"
    base = re.sub(r"_part(-\d+)?$", "", src.stem)
    out_dir.mkdir(parents=True, exist_ok=True)
    out, n = out_dir / f"{base}_part{ext}", 2
    while out.exists():
        out, n = out_dir / f"{base}_part-{n}{ext}", n + 1
    if is_video:
        dur = end - start
        fade = min(EDGE_FADE, dur / 2)
        filter_str = (
            f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[outv];"
            f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS,"
            f"afade=t=in:d={fade:.4f},areverse,afade=t=in:d={fade:.4f},areverse[outa]"
        )
        _ffmpeg("-i", str(src), "-filter_complex", filter_str,
                "-map", "[outv]", "-map", "[outa]", *VIDEO_CODEC, str(out))
    else:
        render([Clip(src, start, end, 1.0)], out, ["-c:a", "pcm_s24le"])
    try:
        prev = json.loads(src.with_suffix(".json").read_text())
    except (OSError, ValueError):
        prev = {}
    data = {
        "source": str(src), "output": str(out), "created_at": datetime.now().isoformat(timespec="seconds"),
        "part": True, "range": [round(start, 3), round(end, 3)], "duration_sec": round(probe_duration(out), 2),
        "is_video": is_video,
        **{k: prev[k] for k in ("threshold_db", "speech_db") if k in prev},
    }
    out.with_suffix(".json").write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return data


def _export_path(root: Path, fmt: str, tag: str = "") -> Path:
    now = datetime.now()
    day_dir = root / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    base = f"edit_{tag}{now.strftime('%H%M%S')}"
    out = day_dir / f"{base}.{fmt}"
    n = 2
    while out.exists():
        out = day_dir / f"{base}-{n}.{fmt}"
        n += 1
    return out


ENHANCE_SHARE = 0.6  # video export: share of the progress bar for enhancing the audio; the encode gets the rest


def _render_video(clips: list[Clip], audio: Path, out: Path, tmp: Path, progress: Progress | None) -> None:
    """Puts the timeline's picture under its (rendered, maybe enhanced) audio track → MP4.

    Each clip's picture is trimmed, sped up like its audio, scaled and padded to the first video
    clip's size; audio-only clips get a black picture. Every clip gets exactly as many frames as its
    audio lasts (atempo's length differs from (end - start) / speed by up to ~20 ms, so it is
    measured, not computed), rounded on the running total so the picture never drifts from the sound.
    """
    clips = merge_continuous(clips)  # as render() does: segments match the audio's one for one
    ref = next(c for c in clips if has_video_stream(c.path))
    info = probe_video_info(ref.path)
    w, h, fps = info["width"], info["height"], Fraction(info["fps"])
    durations = []
    for i, c in enumerate(clips):  # each clip's audio renders on its own exactly as inside the join
        part = tmp / f"clip_{i}.wav"
        render([c], part, ["-c:a", "pcm_f32le"])
        durations.append(probe_duration(part))
        part.unlink()
    counts = _frame_counts(durations, fps)
    inputs, parts = [], []
    for i, (c, n) in enumerate(zip(clips, counts)):
        if has_video_stream(c.path):
            inputs += ["-i", str(c.path)]
            idx = len(inputs) // 2 - 1
            chain = f"[{idx}:v]trim=start={c.start:.3f}:end={c.end:.3f},setpts=PTS-STARTPTS"
            if abs(c.speed - 1.0) > 1e-3:
                chain += f",setpts=PTS/{c.speed:.3f}"
            chain += (f",fps=fps={fps}:start_time=0,scale={w}:{h}:force_original_aspect_ratio=decrease,"
                      f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,"
                      f"tpad=stop_mode=clone:stop={n},trim=end_frame={n},setpts=PTS-STARTPTS[v{i}]")
        else:
            chain = (f"color=c=black:s={w}x{h}:r={fps},format=yuv420p,setsar=1,"
                     f"trim=end_frame={n},setpts=PTS-STARTPTS[v{i}]")
        parts.append(chain)
    parts.append("".join(f"[v{i}]" for i in range(len(clips))) + f"concat=n={len(clips)}:v=1:a=0[outv]")
    inputs += ["-i", str(audio)]
    script = tmp / "timeline_video_filter.txt"
    script.write_text(";".join(parts), encoding="utf-8")
    video = tmp / "video.mp4"  # moved into place when done: a cancel leaves no half-written file
    ffmpeg_progress([*inputs, "-/filter_complex", str(script), "-map", "[outv]", "-map", f"{len(inputs) // 2 - 1}:a",
                     *VIDEO_CODEC, str(video)], sum(durations), progress)
    shutil.move(str(video), str(out))


def export_timeline(clips: list[Clip], enhance_it: bool = True, opts: EnhanceOptions | None = None,
                    log: Callable[[str], None] = print, progress: Progress | None = None) -> dict:
    """Renders the timeline, enhancing it if asked; output + JSON report go to outputs/YYYY-MM-DD/."""
    opts = opts or EnhanceOptions()
    has_video = any(has_video_stream(c.path) for c in clips)
    if opts.fmt not in CODECS:
        raise PipelineError(f"Unknown format: {opts.fmt}")
    fmt = "mp4" if has_video else opts.fmt
    out = _export_path(resolve_outputs(opts), fmt, f"{opts.denoiser}_" if enhance_it and opts.denoiser in GENERATIVE else "")

    with tempfile.TemporaryDirectory(prefix="soundlab-edit-") as tmp:
        joined = Path(tmp) / "joined.wav"
        render(clips, joined, ["-c:a", "pcm_f32le"])
        if has_video:
            report = progress or (lambda _: None)
            if enhance_it:
                enhanced_audio = Path(tmp) / "enhanced.wav"
                data = enhance(joined, opts, log=log, out=enhanced_audio,
                               progress=lambda f: report(ENHANCE_SHARE * f)).data
                data["source"] = "timeline"
            else:
                enhanced_audio = joined
                data = {"source": "timeline"}
            start = ENHANCE_SHARE if enhance_it else 0.0
            _render_video(clips, enhanced_audio, out, Path(tmp),
                          lambda f: report(start + (1.0 - start) * f) if progress else None)
            data["output"] = str(out)
            data["duration_sec"] = round(probe_duration(out), 2)
            data["is_video"] = True
        else:
            if enhance_it:
                data = enhance(joined, opts, log=log, out=out, progress=progress).data
                data["source"] = "timeline"
            else:
                _ffmpeg("-i", str(joined), *CODECS[opts.fmt], str(out))
                data = {"source": "timeline", "output": str(out), "duration_sec": round(probe_duration(out), 2)}

    data["clips"] = [{**asdict(c), "path": str(c.path)} for c in clips]
    data["enhanced"] = enhance_it
    out.with_suffix(".json").write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return data
