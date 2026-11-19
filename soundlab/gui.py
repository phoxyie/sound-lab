"""Local web UI: projects, recording, media (import), editing (trim/speed) and joining.

Binds to 127.0.0.1 only; all processing runs in the backend with ffmpeg.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import webbrowser
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable

from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from werkzeug.serving import make_server

from . import projects
from .editor import (PAUSE_MAX, Clip, export_timeline, extract, has_audio_stream, has_video_stream,
                    keep_segments, render, tighten)
from .pipeline import (CODECS, PROJECT_ROOT, RESEMBLE_NFE_MAX, RESEMBLE_RATE_A, RESEMBLE_RATE_B, RESEMBLE_SEED_MAX,
                       RESEMBLE_SOLVERS, Cancelled,
                       EnhanceOptions, PipelineError, Progress, ResembleSettings, _ffmpeg, enhance, find_resemble,
                       find_sidon, measure_loudness)
from .presets import PRESETS
from .recorder import Recorder, list_audio_devices, pick_device

WEB_DIR = Path(__file__).resolve().parent / "web"
# Overridable via environment so tests don't touch the real Trash or open Finder
# Windows: a folder of our own in the user's home (the Recycle Bin needs a shell API; the temp folder
# gets emptied by the system, which would make "delete" final)
_default_trash = Path("~/SoundLab Trash").expanduser() if os.name == "nt" else Path("~/.Trash").expanduser()
TRASH_DIR = Path(os.environ.get("SOUNDLAB_TRASH", _default_trash)).expanduser()
_default_reveal = ["explorer", "/select,"] if os.name == "nt" else ["open", "-R"]
REVEAL_CMD = os.environ.get("SOUNDLAB_REVEAL_CMD", "").split() if "SOUNDLAB_REVEAL_CMD" in os.environ else _default_reveal
# Some browsers won't play the types the system guesses (e.g. audio/mp4a-latm)
MIME_TYPES = {".wav": "audio/wav", ".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".aiff": "audio/aiff",
              ".aif": "audio/aiff", ".flac": "audio/flac", ".aac": "audio/aac", ".caf": "audio/x-caf",
              ".ogg": "audio/ogg"}
AUDIO_EXTS = set(MIME_TYPES)
VIDEO_MIME_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".m4v": "video/x-m4v",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
}
VIDEO_EXTS = set(VIDEO_MIME_TYPES)
MEDIA_EXTS = AUDIO_EXTS | VIDEO_EXTS
ALL_MIME_TYPES = {**MIME_TYPES, **VIDEO_MIME_TYPES}
TARGET_MIN, TARGET_MAX = -30.0, -9.0
# Timeline previews: one WAV with trim/speed applied (no enhancement), cached by a content hash
PREVIEW_DIR = Path(tempfile.gettempdir()) / "soundlab-preview"
AUDIO_PROXY_DIR = Path(tempfile.gettempdir()) / "soundlab-audio-proxy"  # a video's sound for the waveform
PREVIEW_KEEP = 20
JOBS_KEEP = 20  # finished jobs kept for status polling
GUI_DENOISERS = ["auto", "resemble", "sidon"]  # auto = DeepFilterNet (afftdn if it's missing)
# Resemble jobs estimated to take longer than this ask first (overridable so the e2e test can see the dialog)
RESEMBLE_CONFIRM_SEC = float(os.environ.get("SOUNDLAB_RESEMBLE_CONFIRM_SEC", "60"))

app = Flask(__name__, static_folder=str(WEB_DIR), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024**3  # import size limit: 2 GB
recorder = Recorder()
_lock = threading.Lock()
_preview_lock = threading.Lock()
_proxy_lock = threading.Lock()
_duration_cache: dict[tuple[str, float], float] = {}
# Enhance / export run as background jobs (Resemble takes minutes): the browser polls /api/jobs/<id>
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _rel(p: Path) -> str:
    return p.resolve().relative_to(PROJECT_ROOT).as_posix()


def _audio_proxy(p: Path) -> Path:
    """A video's sound as a cached 48 kHz 16-bit WAV, for the waveform and playback in the browser."""
    AUDIO_PROXY_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{p}:{p.stat().st_mtime}".encode()).hexdigest()[:16]
    proxy = AUDIO_PROXY_DIR / f"{key}.wav"
    with _proxy_lock:  # one extraction per file at a time
        if not proxy.exists():
            tmp_proxy = proxy.with_suffix(".tmp.wav")
            _ffmpeg("-i", str(p), "-vn", "-c:a", "pcm_s16le", "-ar", "48000", str(tmp_proxy))
            tmp_proxy.replace(proxy)
            # temp copies, not user files: keep the newest few like the timeline previews
            for old in sorted(AUDIO_PROXY_DIR.glob("*.wav"), key=lambda f: f.stat().st_mtime)[:-PREVIEW_KEEP]:
                if old != proxy:
                    old.unlink(missing_ok=True)
    return proxy


def _resolve_media(rel: str) -> Path:
    """Resolves a client path, allowing only media files inside the current project's folders."""
    p = (PROJECT_ROOT / rel).resolve()
    roots = [d.resolve() for d in projects.current().dirs]
    if not any(p.is_relative_to(r) for r in roots) or p.suffix.lower() not in MEDIA_EXTS or not p.is_file():
        raise PipelineError(f"Invalid file: {rel}")
    return p


def _duration(p: Path) -> float | None:
    key = (str(p), p.stat().st_mtime)
    if key not in _duration_cache:
        proc = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                               "-of", "default=nw=1:nk=1", str(p)], capture_output=True, text=True)
        try:
            _duration_cache[key] = round(float(proc.stdout.strip()), 2)
        except ValueError:
            return None
    return _duration_cache[key]


@app.errorhandler(PipelineError)
@app.errorhandler(projects.ProjectError)
def _user_error(e: Exception):
    return jsonify(error=str(e)), 400


@app.get("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


def _is_enhanced(p: Path) -> bool:
    """Is an output enhanced? Checks its .json report; without one, anything in outputs/ counts as processed."""
    try:
        data = json.loads(p.with_suffix(".json").read_text())
    except (OSError, ValueError):
        return True
    return bool(data.get("enhanced", "denoiser" in data))


def _scan(roots: list[Path], skip_prefix: str | None = None, processed: bool = False) -> list[dict]:
    files = []
    for root in roots:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            suf = p.suffix.lower()
            if suf not in MEDIA_EXTS or not p.is_file():
                continue
            rel_parts = p.relative_to(root).parts
            if skip_prefix and any(part.startswith(skip_prefix) for part in rel_parts[:-1]):
                continue
            files.append({
                "path": _rel(p), "name": p.name, "source": root.name,
                "folder": "/".join(rel_parts[:-1]),
                "duration": _duration(p), "mtime": p.stat().st_mtime,
                "enhanced": processed and _is_enhanced(p),
                "is_video": suf in VIDEO_EXTS,
            })
    files.sort(key=lambda f: f["mtime"], reverse=True)
    return files


@app.get("/api/library")
def library():
    # Media: imported + recorded files. Exports are separate; compare sets are hidden.
    proj = projects.current()
    return jsonify(project=proj.name, groups=[
        {"id": "media", "name": "Media", "files": _scan([proj.inputs, proj.records])},
        {"id": "exports", "name": "Exports", "files": _scan([proj.outputs], skip_prefix="compare_", processed=True)},
    ])


def _projects_payload() -> dict:
    items = []
    for name in projects.list_projects():
        dirs = projects.Project(name).dirs
        count = sum(1 for d in dirs if d.is_dir() for f in d.rglob("*") if f.suffix.lower() in MEDIA_EXTS)
        items.append({"name": name, "files": count})
    return {"projects": items, "current": projects.current().name}


def _not_while_recording() -> None:
    if recorder.running:
        raise PipelineError("Can't change projects while recording")


@app.get("/api/projects")
def list_projects():
    return jsonify(_projects_payload())


@app.post("/api/projects")
def create_project():
    with _lock:
        _not_while_recording()
        projects.create((request.get_json(silent=True) or {}).get("name", ""))
    return jsonify(_projects_payload())


@app.post("/api/projects/current")
def switch_project():
    with _lock:
        _not_while_recording()
        projects.set_current((request.get_json(silent=True) or {}).get("name", ""))
    return jsonify(_projects_payload())


@app.post("/api/projects/rename")
def rename_project():
    body = request.get_json(silent=True) or {}
    with _lock:
        _not_while_recording()
        old = projects.Project(body.get("name", ""))
        old_prefix = _rel(old.root) if old.root.is_dir() else None
        new = projects.rename(old.name, body.get("new_name", ""))
    # The browser keeps timelines per project name with paths inside the folder: it rewrites these prefixes
    return jsonify({**_projects_payload(), "renamed": {"from": old.name, "to": new.name,
                                                       "old_prefix": old_prefix, "new_prefix": _rel(new.root)}})


@app.post("/api/projects/delete")
def delete_project():
    """Moves the whole project folder to the Trash."""
    with _lock:
        _not_while_recording()
        projects.delete((request.get_json(silent=True) or {}).get("name", ""), TRASH_DIR)
    return jsonify(_projects_payload())


def _unique(dest: Path) -> Path:
    n = 2
    out = dest
    while out.exists():
        out = dest.with_name(f"{dest.stem}-{n}{dest.suffix}")
        n += 1
    return out


@app.post("/api/import")
def import_files():
    """Copies files sent by the browser into the project's inputs/ folder (the browser can't see the original path)."""
    target = projects.current().inputs
    target.mkdir(parents=True, exist_ok=True)
    imported, rejected = [], []
    for f in request.files.getlist("files"):
        name = "".join(ch for ch in Path(f.filename or "").name if ch not in '/\\:\0').strip() or "import"
        suffix = Path(name).suffix.lower()
        if suffix not in MEDIA_EXTS:
            rejected.append(f"{name} (unsupported type)")
            continue
        dest = _unique(target / f"{Path(name).stem[:100]}{suffix}")
        f.save(dest)
        if _duration(dest) is None:  # ffprobe can't read it: broken file or wrong extension
            dest.unlink(missing_ok=True)
            rejected.append(f"{name} (unreadable)")
            continue
        if suffix in VIDEO_EXTS and not has_audio_stream(dest):
            dest.unlink(missing_ok=True)
            rejected.append(f"{name} (no audio track in video)")
            continue
        imported.append(_rel(dest))
    if not imported and rejected:
        raise PipelineError("Import failed: " + ", ".join(rejected))
    return jsonify(imported=imported, rejected=rejected)


@app.post("/api/trash")
def trash():
    """Moves the file (and its .json report, if any) to the Trash (TRASH_DIR)."""
    p = _resolve_media((request.get_json(silent=True) or {}).get("path", ""))
    if recorder.running and recorder.out and recorder.out.resolve() == p:
        raise PipelineError("Can't delete a file while it is being recorded")
    rel = _rel(p)
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    for f in [p, p.with_suffix(".json")]:
        if f.is_file():
            shutil.move(str(f), str(_unique(TRASH_DIR / f.name)))
    return jsonify(trashed=rel)


@app.post("/api/reveal")
def reveal():
    p = _resolve_media((request.get_json(silent=True) or {}).get("path", ""))
    subprocess.run([*REVEAL_CMD, str(p)], check=False)
    return jsonify(ok=True)


@app.get("/api/audio")
def audio():
    try:
        p = _resolve_media(request.args.get("path", ""))
    except PipelineError:
        abort(404)
    if p.suffix.lower() in VIDEO_EXTS:
        proxy = _audio_proxy(p)
        return send_file(proxy, mimetype="audio/wav", conditional=True)
    return send_file(p, mimetype=MIME_TYPES[p.suffix.lower()], conditional=True)


@app.get("/api/devices")
def devices():
    refresh = not recorder.running  # re-scanning devices would cut a running recording
    devs = list_audio_devices(refresh)
    return jsonify(devices=devs, default=pick_device(refresh=False) if devs else None)


def _record_status() -> dict:
    return {
        "recording": recorder.running,
        "path": _rel(recorder.out) if recorder.running and recorder.out else None,
        "device": recorder.device if recorder.running else None,
        "elapsed": (datetime.now() - recorder.started_at).total_seconds() if recorder.running else 0,
        "level": recorder.level if recorder.running else None,
        "peak_max": recorder.peak_max if recorder.running else None,
    }


@app.get("/api/record")
def record_status():
    return jsonify(_record_status())


@app.post("/api/record/start")
def record_start():
    body = request.get_json(silent=True) or {}
    with _lock:
        recorder.start(body.get("device") or None, root=projects.current().records, name=body.get("name"))
    return jsonify(_record_status())


@app.post("/api/record/mark")
def record_mark():
    """End of the countdown: what was recorded before it (minus a short lead-in) is cut on stop."""
    with _lock:
        at = recorder.mark()
    return jsonify(at=round(at, 3))


@app.post("/api/record/stop")
def record_stop():
    with _lock:
        info = recorder.stop()
    return jsonify(path=_rel(info["path"]), duration=round(info["duration"], 2), lufs=info["lufs"],
                   true_peak=info["true_peak"], warnings=info["warnings"], countdown_cut=info["countdown_cut"])


@app.post("/api/record/cancel")
def record_cancel():
    """Stops a recording that isn't wanted (cancelled countdown) and moves it to the Trash."""
    with _lock:
        out = recorder.cancel()
        TRASH_DIR.mkdir(parents=True, exist_ok=True)
        if out.is_file():
            shutil.move(str(out), str(_unique(TRASH_DIR / out.name)))
    return jsonify(cancelled=True)


@app.get("/api/config")
def config():
    return jsonify(presets=list(PRESETS), default_preset=EnhanceOptions.preset,
                   default_target=EnhanceOptions.target_i, default_tp=EnhanceOptions.target_tp,
                   default_min_lufs=EnhanceOptions.min_lufs, formats=list(CODECS),
                   resemble={"installed": find_resemble() is not None, "defaults": asdict(ResembleSettings()),
                             "solvers": RESEMBLE_SOLVERS, "nfe_max": RESEMBLE_NFE_MAX, "seed_max": RESEMBLE_SEED_MAX,
                             "rate": [RESEMBLE_RATE_A, RESEMBLE_RATE_B], "confirm_above_sec": RESEMBLE_CONFIRM_SEC},
                   sidon={"installed": find_sidon() is not None})


def _resemble_settings(raw: dict | None) -> ResembleSettings:
    d = ResembleSettings()
    raw = raw or {}
    try:
        rs = ResembleSettings(tau=float(raw.get("tau", d.tau)), nfe=int(raw.get("nfe", d.nfe)),
                              lambd=float(raw.get("lambd", d.lambd)), solver=str(raw.get("solver", d.solver)),
                              seed=int(raw.get("seed", d.seed)))
    except (TypeError, ValueError):
        raise PipelineError("Invalid Resemble settings") from None
    rs.validate()
    return rs


def _enhance_options(body: dict) -> EnhanceOptions:
    fmt = body.get("format", "m4a")
    if fmt not in CODECS:
        raise PipelineError(f"Unknown format: {fmt}")
    preset = body.get("preset", EnhanceOptions.preset)
    if preset not in PRESETS:
        raise PipelineError(f"Unknown preset: {preset}")
    target = float(body.get("target", EnhanceOptions.target_i))
    if not (TARGET_MIN <= target <= TARGET_MAX):
        raise PipelineError(f"Target loudness must be between {TARGET_MIN} and {TARGET_MAX} LUFS")
    min_lufs = float(body.get("min_lufs", EnhanceOptions.min_lufs))
    if not (-100.0 <= min_lufs <= -30.0):
        raise PipelineError("Minimum loudness threshold must be between -100 and -30 LUFS")
    denoiser = body.get("denoiser", "auto")
    if denoiser not in GUI_DENOISERS:
        raise PipelineError(f"Unknown denoiser: {denoiser}")
    if denoiser == "resemble" and not find_resemble():
        raise PipelineError("Resemble Enhance isn't installed: run ./scripts/install_resemble.sh")
    if denoiser == "sidon" and not find_sidon():
        raise PipelineError("Sidon isn't installed: run ./scripts/install_sidon.sh")
    return EnhanceOptions(preset=preset, target_i=target, min_lufs=min_lufs, fmt=fmt,
                          outputs_root=projects.current().outputs,
                          denoiser=denoiser, resemble=_resemble_settings(body.get("resemble")))


def _start_job(work: Callable[[Progress], dict]) -> str:
    """Runs work(progress) in a thread; progress raises Cancelled once the job is cancelled."""
    job = {"id": uuid.uuid4().hex[:12], "status": "running", "progress": 0.0, "result": None, "error": None,
           "started": time.monotonic(), "cancel": threading.Event()}

    def progress(f: float) -> None:
        if job["cancel"].is_set() and f < 1:  # at 1 the output is written: let it finish
            raise Cancelled("Cancelled")
        job["progress"] = round(f, 3)

    def run() -> None:
        try:
            job["result"] = work(progress)
            job["progress"], job["status"] = 1.0, "done"
        except Cancelled:
            job["status"] = "cancelled"
        except (PipelineError, projects.ProjectError) as e:
            job["error"], job["status"] = str(e), "error"
        except Exception as e:  # noqa: BLE001 — report it in the UI instead of a silently dead job
            logging.getLogger(__name__).exception("job failed")
            job["error"], job["status"] = f"Unexpected error: {e}", "error"

    with _jobs_lock:
        finished = [j for j in _jobs.values() if j["status"] != "running"]
        for old in sorted(finished, key=lambda j: j["started"])[:-JOBS_KEEP]:
            del _jobs[old["id"]]
        _jobs[job["id"]] = job
    threading.Thread(target=run, daemon=True).start()
    return job["id"]


def _job(job_id: str) -> dict:
    job = _jobs.get(job_id)
    if job is None:
        abort(404)
    return job


@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    job = _job(job_id)
    return jsonify(id=job["id"], status=job["status"], progress=job["progress"], result=job["result"],
                   error=job["error"], elapsed=round(time.monotonic() - job["started"], 1))


@app.post("/api/jobs/<job_id>/cancel")
def job_cancel(job_id: str):
    _job(job_id)["cancel"].set()
    return jsonify(ok=True)


def _result(data: dict) -> dict:
    return {
        "path": _rel(Path(data["output"])),
        "duration": data.get("duration_sec"),
        "lufs": data.get("loudness_output", {}).get("lufs"),
        "snr_in": data.get("noise_input", {}).get("snr_db"),
        "snr_out": data.get("noise_output", {}).get("snr_db"),
        "enhanced": data.get("enhanced", True),
    }


@app.post("/api/enhance")
def enhance_file():
    """Starts enhancing one file (only the selected range if trim/speed were changed); returns a job id."""
    body = request.get_json(force=True)
    src = _resolve_media(body.get("path", ""))
    opts = _enhance_options(body)
    start, end = float(body.get("start", 0)), body.get("end")
    speed = float(body.get("speed", 1.0))
    duration = _duration(src) or 0
    end = duration if end is None else float(end)
    untouched = start <= 0.01 and end >= duration - 0.01 and abs(speed - 1) < 1e-3

    def work(progress: Progress) -> dict:
        if untouched and not has_video_stream(src):
            data = enhance(src, opts, log=lambda _: None, progress=progress).data
        else:
            data = export_timeline([Clip(src, start, end, speed)], enhance_it=True, opts=opts,
                                   log=lambda _: None, progress=progress)
        return _result(data)

    return jsonify(job=_start_job(work))


@app.post("/api/tighten")
def tighten_file():
    """New "<name>_tight.wav" in the project's inputs/: the track's range with the silence at both ends
    trimmed and long inner pauses shortened (the source is untouched)."""
    body = request.get_json(silent=True) or {}
    src = _resolve_media(body.get("path", ""))
    max_pause = float(body.get("max_pause", PAUSE_MAX))
    if not (PAUSE_MAX <= max_pause <= 5):
        raise PipelineError(f"Pause limit must be between {PAUSE_MAX} and 5 seconds")
    start = float(body.get("start", 0))
    end = float(body.get("end") or _duration(src) or 0)
    data = tighten(src, start, end, projects.current().inputs, max_pause)
    if data is None:
        return jsonify(unchanged=True)
    return jsonify(path=_rel(Path(data["output"])), duration=data["duration_sec"], removed=data["removed_sec"],
                   shortened=data["pauses_shortened"], trimmed=data["trimmed_sec"])


@app.post("/api/extract")
def extract_part():
    """New "<name>_part.wav" in the project's inputs/: the track's range as a recording of its own."""
    body = request.get_json(silent=True) or {}
    src = _resolve_media(body.get("path", ""))
    data = extract(src, float(body.get("start", 0)), float(body.get("end") or _duration(src) or 0),
                   projects.current().inputs)
    return jsonify(path=_rel(Path(data["output"])), duration=data["duration_sec"])


def _clips(body: dict) -> list[Clip]:
    return [Clip(path=_resolve_media(c["path"]), start=float(c["start"]), end=float(c["end"]),
                 speed=float(c.get("speed", 1.0))) for c in body.get("clips", [])]


@app.post("/api/preview")
def preview():
    """Renders the timeline into one file (same trim/speed as the export, no enhancement).

    Like an editor's "render preview": the browser plays a single file, so there are no gaps
    between clips. The same content returns the cached file.
    """
    clips = _clips(request.get_json(force=True))
    key_src = json.dumps([[str(c.path), c.path.stat().st_mtime, c.start, c.end, c.speed] for c in clips])
    name = hashlib.sha1(key_src.encode()).hexdigest()[:16] + ".wav"
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    out = PREVIEW_DIR / name
    with _preview_lock:  # don't render the same preview twice at once
        if not out.exists():
            tmp = out.with_name(out.stem + ".tmp.wav")
            render(clips, tmp, ["-c:a", "pcm_s16le"])
            tmp.replace(out)
            for old in sorted(PREVIEW_DIR.glob("*.wav"), key=lambda f: f.stat().st_mtime)[:-PREVIEW_KEEP]:
                old.unlink(missing_ok=True)
                old.with_suffix(".json").unlink(missing_ok=True)
    return jsonify(url=f"/api/preview/{name}", duration=_duration(out))


def _report_clips(p: Path, data: dict) -> list[Clip]:
    """The source clips an enhanced output was made from, with paths remapped if the project moved.

    Reports keep absolute paths; after a project rename (or moving the repo) they point at the old
    folder, so they are re-based on where this output lives now.
    """
    root = projects.current().root.resolve()
    old_out = Path(data.get("output", ""))
    rel_out = p.resolve().relative_to(root)
    old_root = (Path(*old_out.parts[:-len(rel_out.parts)])
                if old_out.parts[-len(rel_out.parts):] == rel_out.parts else root)

    def rebase(path: str) -> Path:
        src = Path(path)
        return root / src.relative_to(old_root) if src.is_relative_to(old_root) else src

    if data.get("clips"):
        raw = [(rebase(c["path"]), float(c["start"]), float(c["end"]), float(c.get("speed", 1)))
               for c in data["clips"]]
    elif data.get("source") not in (None, "timeline"):
        src = rebase(data["source"])
        raw = [(src, 0.0, (_duration(src) if src.is_file() else None) or 0.0, 1.0)]
    else:
        raise PipelineError(f"The report of {p.name} doesn't say what it was made from")
    try:
        return [Clip(path=_resolve_media(_rel(path)), start=start, end=end, speed=speed)
                for path, start, end, speed in raw]
    except (PipelineError, ValueError, OSError):
        raise PipelineError(f"Can't find the original of {p.name} (moved or deleted)") from None


@app.post("/api/original")
def original():
    """The unprocessed counterpart of an enhanced output, for A/B listening.

    Rendered from the same clips (same trim / speed, so positions line up 1:1) without enhancement
    and raised to the enhanced file's loudness, so the comparison isn't just "louder sounds better".
    """
    p = _resolve_media((request.get_json(silent=True) or {}).get("path", ""))
    try:
        data = json.loads(p.with_suffix(".json").read_text())
    except (OSError, ValueError):
        raise PipelineError(f"{p.name} has no report, so its original is unknown") from None
    if not data.get("enhanced", "denoiser" in data):
        raise PipelineError(f"{p.name} isn't enhanced")
    clips = _report_clips(p, data)
    key_src = json.dumps(["ab", str(p), p.stat().st_mtime]
                         + [[str(c.path), c.path.stat().st_mtime, c.start, c.end, c.speed] for c in clips])
    name = "ab_" + hashlib.sha1(key_src.encode()).hexdigest()[:16] + ".wav"
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    out = PREVIEW_DIR / name
    gain_file = out.with_suffix(".json")  # the applied gain, for the button label
    with _preview_lock:
        if not out.exists() or not gain_file.exists():
            with tempfile.TemporaryDirectory(prefix="soundlab-ab-") as tmp:
                raw = Path(tmp) / "raw.wav"
                render(clips, raw, ["-c:a", "pcm_f32le"])
                keep = (data.get("pauses") or {}).get("keep")
                if keep:  # exports made with the old "Tighten pauses" option: cut the original the same way
                    tight = Path(tmp) / "tight.wav"
                    keep_segments(raw, keep, tight, ["-c:a", "pcm_f32le"])
                    raw = tight
                target = measure_loudness(p)["lufs"]
                raw_lufs = measure_loudness(raw)["lufs"]
                if target <= -90.0 or raw_lufs <= -90.0:
                    gain = 0.0
                else:
                    gain = max(-40.0, min(40.0, target - raw_lufs))
                tmp_out = out.with_name(out.stem + ".tmp.wav")
                # The limiter keeps the raised original from clipping (-1 dBFS ceiling) but takes some
                # loudness back, so measure again and make up the difference
                for _ in range(3):
                    _ffmpeg("-i", str(raw), "-af", f"volume={gain:.2f}dB,alimiter=limit=0.891:level=false",
                            "-c:a", "pcm_s16le", str(tmp_out))
                    out_lufs = measure_loudness(tmp_out)["lufs"]
                    if out_lufs <= -90.0:
                        break
                    miss = target - out_lufs
                    if abs(miss) < 0.3:
                        break
                    gain += max(-10.0, min(10.0, miss))
                tmp_out.replace(out)
                gain_file.write_text(json.dumps({"gain_db": round(gain, 1)}))
    return jsonify(url=f"/api/preview/{name}", duration=_duration(out),
                   gain=json.loads(gain_file.read_text())["gain_db"])


@app.get("/api/preview/<name>")
def preview_file(name: str):
    p = PREVIEW_DIR / Path(name).name
    if p.suffix != ".wav" or not p.is_file():
        abort(404)
    return send_file(p, mimetype="audio/wav", conditional=True)


@app.post("/api/export")
def export():
    """Starts joining (and enhancing) the timeline; returns a job id."""
    body = request.get_json(force=True)
    clips, enhance_it, opts = _clips(body), bool(body.get("enhance", True)), _enhance_options(body)
    if not clips:
        raise PipelineError("The timeline is empty")
    return jsonify(job=_start_job(lambda progress: _result(
        export_timeline(clips, enhance_it=enhance_it, opts=opts, log=lambda _: None, progress=progress))))


def run(port: int = 8765, open_browser: bool = True) -> None:
    url = f"http://127.0.0.1:{port}"
    # make_server instead of app.run(): no "development server" warning or request logs.
    # The server is reachable from this machine only (127.0.0.1).
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    server = make_server("127.0.0.1", port, app, threaded=True)
    print(f"Sound Lab GUI: {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=[url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if recorder.running:
            recorder.stop()
        server.server_close()
