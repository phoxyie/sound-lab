"""Mic recording: CoreAudio (sounddevice) → lossless 24-bit WAV → <project>/voice-records/YYYY-MM-DD/

Captured with PortAudio / CoreAudio rather than ffmpeg: ffmpeg's avfoundation input dropped ~7% of
the 512-sample blocks the mic delivers (an 8 s take came out 7.4 s long, with a click at every gap).
Samples are appended to a raw float32 file while recording; on stop, ffmpeg turns it into the WAV
(and cuts a countdown off) — ffmpeg only converts, it never captures.
"""

from __future__ import annotations

import math
import os
import queue
import re
import threading
import time
from array import array
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import projects
from .pipeline import PipelineError, _ffmpeg, measure_loudness, probe_duration

CLIP_WARN_TP = -1.0     # true peak above this: likely clipping
QUIET_WARN_LUFS = -40.0  # loudness below this: input level too low
LEAD_IN = 0.3           # kept before the end of a countdown, so the first syllable is never cut
NAME_MAX = 60
METER_WINDOW = 0.05     # seconds per live level reading
# Tests replace the microphone with a generated tone: "tone" or "tone:<Hz>" (real-time, -18 dBFS peak)
TEST_INPUT = os.environ.get("SOUNDLAB_RECORD_INPUT")


def _sd():
    import sounddevice  # loaded on demand: commands that don't record work without it
    return sounddevice


def list_audio_devices(refresh: bool = True) -> list[str]:
    """Input devices by name. refresh re-scans (AirPods connected later); not while recording."""
    sd = _sd()
    if refresh:
        sd._terminate()
        sd._initialize()
    return [d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0]


def pick_device(query: str | None = None, refresh: bool = True) -> str:
    devices = list_audio_devices(refresh)
    if not devices:
        raise PipelineError("No audio input device found")
    if query is None:
        # Prefer the built-in mic; unaffected by index shifts when AirPods etc. are connected
        return next((d for d in devices if "macbook" in d.lower()), devices[0])
    if query.isdigit() and int(query) < len(devices):
        return devices[int(query)]
    match = [d for d in devices if query.lower() in d.lower()]
    if not match:
        raise PipelineError(f"Device not found: {query} (available: {', '.join(devices)})")
    return match[0]


def take_name(name: str | None) -> str | None:
    """A take name usable as a file name: letters, digits, spaces, - and _ (None if nothing is left)."""
    name = re.sub(r"\s+", " ", re.sub(r"[^\w \-]", "", name or "")).strip()[:NAME_MAX].strip()
    return name or None


def _record_path(root: Path | None, name: str | None = None) -> Path:
    root = root or projects.current().records
    now = datetime.now()
    day_dir = root / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    base = take_name(name) or f"rec_{now.strftime('%H%M%S')}"
    out = day_dir / f"{base}.wav"
    n = 2
    while out.exists():
        out = day_dir / f"{base}-{n}.wav"
        n += 1
    return out


def finalize_recording(out: Path, dropouts: int = 0) -> dict:
    """Validates the recording and measures levels; returns a summary with warnings."""
    if not out.exists() or out.stat().st_size <= 44:
        out.unlink(missing_ok=True)
        raise PipelineError("No recording was created (does the terminal have microphone access? "
                            "System Settings → Privacy & Security → Microphone)")
    lvl = measure_loudness(out)
    warnings = []
    if lvl["true_peak"] > CLIP_WARN_TP:
        warnings.append("Peaks are very high, possible clipping: lower the input level")
    elif lvl["lufs"] < QUIET_WARN_LUFS:
        warnings.append("The recording is very quiet (or there is no mic permission and silence was recorded)")
    if dropouts:
        warnings.append(f"The computer fell behind {dropouts} time(s): there may be short gaps")
    return {"path": out, "duration": probe_duration(out), **lvl, "warnings": warnings}


class _ToneStream:
    """Stands in for the mic in tests: a sine in 512-frame blocks at real-time pace, same callback."""

    def __init__(self, samplerate: int, callback: Callable, freq: float):
        self.samplerate, self.callback, self.freq = samplerate, callback, freq
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        n, block, step = 0, 512, 2 * math.pi * self.freq / self.samplerate
        started = time.monotonic()
        while not self._stop.is_set():
            data = array("f", (0.125 * math.sin(step * (n + i)) for i in range(block)))
            self.callback(data.tobytes(), block, None, None)
            n += block
            time.sleep(max(0.0, started + n / self.samplerate - time.monotonic()))

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def close(self) -> None:
        pass


class Recorder:
    """Start / stop recording for the GUI and CLI, with a live level and a countdown mark.

    The audio callback only queues blocks; a writer thread appends them to <take>.raw and updates
    the level meter, so slow disk or Python work never makes CoreAudio drop input.
    """

    def __init__(self):
        self.out: Path | None = None
        self.device: str | None = None
        self.started_at: datetime | None = None
        self.mark_at: float | None = None  # file position where the countdown ended
        self.level: float | None = None  # latest input peak (dBFS)
        self.peak_max: float | None = None  # loudest peak so far
        self.rate = 48000
        self._stream = None
        self._captured = 0  # frames received so far
        self._dropouts = 0

    @property
    def running(self) -> bool:
        return self._stream is not None

    def start(self, device: str | None = None, root: Path | None = None, name: str | None = None) -> Path:
        if self.running:
            raise PipelineError("Already recording")
        if TEST_INPUT:
            self.device, self.rate = "test tone", 48000
        else:
            sd = _sd()
            self.device = pick_device(device)
            index = next(i for i, d in enumerate(sd.query_devices())
                         if d["name"] == self.device and d["max_input_channels"] > 0)
            self.rate = int(sd.query_devices(index)["default_samplerate"])  # the mic's own rate
        self.out = _record_path(root, name)
        self._raw = self.out.with_suffix(".raw")
        self._file = self._raw.open("wb")
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self.mark_at = self.level = self.peak_max = None
        self._captured = self._dropouts = 0
        self._writer = threading.Thread(target=self._write, daemon=True)
        self._writer.start()
        if TEST_INPUT:
            freq = float(TEST_INPUT.partition(":")[2] or 440)
            self._stream = _ToneStream(self.rate, self._on_audio, freq)
        else:
            self._stream = sd.RawInputStream(device=index, samplerate=self.rate, channels=1,
                                             dtype="float32", callback=self._on_audio)
        try:
            self._stream.start()
        except Exception as e:
            self._stream = None
            self._queue.put(None)
            self._writer.join()
            self._file.close()
            self._raw.unlink(missing_ok=True)
            raise PipelineError(f"Couldn't open the microphone: {e}") from None
        self.started_at = datetime.now()
        return self.out

    def _on_audio(self, data, frames: int, _time, status) -> None:
        # CoreAudio's thread: no waiting here
        if status is not None and status.input_overflow:
            self._dropouts += 1
        self._captured += frames
        self._queue.put(bytes(data))

    def _write(self) -> None:
        window, peak = 0, 0.0
        per_window = int(self.rate * METER_WINDOW)
        while (data := self._queue.get()) is not None:
            self._file.write(data)
            samples = array("f", data)
            peak = max(peak, max(map(abs, samples), default=0.0))
            window += len(samples)
            if window >= per_window:
                self.level = 20 * math.log10(peak) if peak > 1e-6 else -120.0
                self.peak_max = self.level if self.peak_max is None else max(self.peak_max, self.level)
                window, peak = 0, 0.0

    def file_time(self) -> float:
        """How far into the take the recording is right now (seconds)."""
        return self._captured / self.rate

    def mark(self) -> float:
        """Remembers the current position (end of the countdown); stop() cuts what came before."""
        if not self.running:
            raise PipelineError("No active recording")
        self.mark_at = self.file_time()
        return self.mark_at

    def _halt(self, head: float = 0.0) -> Path:
        """Stops capturing and writes the WAV (24-bit, from `head` seconds on); returns its path."""
        if not self.running or self.out is None:
            raise PipelineError("No active recording")
        stream, self._stream = self._stream, None
        stream.stop()
        stream.close()
        self._queue.put(None)
        self._writer.join()
        self._file.close()
        try:
            cut = ["-ss", f"{head:.3f}"] if head > 0.05 else []
            _ffmpeg("-f", "f32le", "-ar", str(self.rate), "-ac", "1", "-i", str(self._raw), *cut,
                    "-c:a", "pcm_s24le", str(self.out))
        finally:
            self._raw.unlink(missing_ok=True)
        self.started_at = None
        return self.out

    def stop(self) -> dict:
        head = max(0.0, (self.mark_at or 0.0) - LEAD_IN)
        out = self._halt(head)
        return {**finalize_recording(out, self._dropouts), "countdown_cut": round(head if head > 0.05 else 0.0, 2)}

    def cancel(self) -> Path:
        """Stops without keeping anything worth keeping (e.g. during the countdown); returns the file."""
        return self._halt()


def record(device: str | None = None, duration: float | None = None,
           root: Path | None = None, log: Callable[[str], None] = print, take: str | None = None) -> Path:
    rec = Recorder()
    out = rec.start(device, root, take)
    log(f"● Recording: {rec.device} → {out}")
    log("  Press Ctrl+C to stop" if not duration else f"  Duration: {duration}s")
    try:
        while not duration or rec.file_time() < duration:
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    info = rec.stop()
    log(f"■ {info['duration']:.1f}s, {info['lufs']:.1f} LUFS, TP {info['true_peak']:.1f} dBTP")
    for w in info["warnings"]:
        log(f"  ! {w}")
    return out
