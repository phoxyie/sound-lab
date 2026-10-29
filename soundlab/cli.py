"""Command-line interface: python -m soundlab enhance <file...>"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import projects
from .compare import compare
from .pipeline import (CODECS, DENOISERS, RESEMBLE_SOLVERS, EnhanceOptions, PipelineError, ResembleSettings,
                       enhance, find_deep_filter, find_resemble)
from .presets import PRESETS
from .recorder import list_audio_devices, record


def _progress_printer():
    """Overall percentage on one line while a slow (Resemble) enhancement runs, if stdout is a terminal."""
    last = [-1]

    def show(f: float) -> None:
        pct = int(f * 100)
        if pct != last[0]:
            last[0] = pct
            print(f"\r    {pct:3d}%", end="\n" if pct >= 100 else "", flush=True)

    return show if sys.stdout.isatty() else None


def cmd_enhance(args: argparse.Namespace) -> int:
    opts = EnhanceOptions(
        preset=args.preset,
        denoiser=args.denoiser,
        atten_lim_db=args.atten,
        resemble=ResembleSettings(tau=args.tau, nfe=args.nfe, lambd=args.lambd, solver=args.solver, seed=args.seed),
        target_i=args.target,
        fmt=args.format,
        outputs_root=args.outputs,
    )
    failed = 0
    for f in args.files:
        print(f"▶ {f}")
        try:
            r = enhance(Path(f), opts, progress=_progress_printer() if opts.denoiser == "resemble" else None)
        except PipelineError as e:
            print(f"  ✗ {e}", file=sys.stderr)
            failed += 1
            continue
        li, lo = r.data["loudness_input"], r.data["loudness_output"]
        print(f"  ✓ {r.output}")
        print(f"    {li['lufs']:.1f} LUFS → {lo['lufs']:.1f} LUFS, TP {lo['true_peak']:.1f} dBTP, "
              f"{r.data['normalization_type']}, {r.data['elapsed_sec']}s")
        ni, no = r.data["noise_input"], r.data["noise_output"]
        if ni["snr_db"] is not None and no["snr_db"] is not None:
            print(f"    SNR {ni['snr_db']:.1f} dB → {no['snr_db']:.1f} dB (noise floor {ni['noise_db']:.1f} dB)")
    return 1 if failed else 0


def cmd_record(args: argparse.Namespace) -> int:
    if args.list_devices:
        for i, d in enumerate(list_audio_devices()):
            print(f"[{i}] {d}")
        return 0
    try:
        path = record(device=args.device, duration=args.duration, root=args.records, take=args.name)
    except PipelineError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    if not args.enhance:
        return 0
    args.files = [str(path)]
    return cmd_enhance(args)


def cmd_compare(args: argparse.Namespace) -> int:
    opts = EnhanceOptions(target_i=args.target, outputs_root=args.outputs)
    try:
        out_dir = compare(Path(args.file), opts)
    except PipelineError as e:
        print(f"  ✗ {e}", file=sys.stderr)
        return 1
    print(f"✓ {out_dir}")
    return 0


def cmd_project(args: argparse.Namespace) -> int:
    try:
        if args.action == "new":
            projects.create(args.name)
        elif args.action == "use":
            projects.set_current(args.name)
        elif args.action == "rename":
            if not args.new_name:
                raise projects.ProjectError("Usage: soundlab project rename OLD NEW")
            projects.rename(args.name, args.new_name)
            print("Note: the GUI timeline of a project only carries over when it's renamed in the GUI\n")
    except projects.ProjectError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    cur = projects.current()
    for name in projects.list_projects():
        print(f"{'●' if name == cur.name else ' '} {name}")
    print(f"\nCurrent project: {cur.root}")
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    from .gui import run  # Flask is only needed for the GUI

    run(port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    ok = True
    for name, path in [("ffmpeg", shutil.which("ffmpeg")), ("ffprobe", shutil.which("ffprobe")),
                       ("deep-filter", find_deep_filter())]:
        print(f"{'✓' if path else '✗'} {name}: {path or 'not found'}")
        ok &= bool(path) or name == "deep-filter"
    resemble = find_resemble()  # optional
    print(f"{'✓' if resemble else '–'} Resemble Enhance (optional): "
          f"{resemble or 'not installed — ./scripts/install_resemble.sh'}")
    try:
        mics = list_audio_devices()
        print(f"✓ sounddevice (recording): {', '.join(mics) or 'no input device'}")
    except (ImportError, OSError) as e:
        print(f"✗ sounddevice (recording): {e} — pip install sounddevice")
        ok = False
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="soundlab", description="Enhances MacBook microphone recordings.")
    sub = p.add_subparsers(dest="cmd", required=True)

    # enhance and record --enhance share the same enhancement options
    enhance_opts = argparse.ArgumentParser(add_help=False)
    enhance_opts.add_argument("--preset", choices=list(PRESETS), default=EnhanceOptions.preset)
    enhance_opts.add_argument("--denoiser", choices=DENOISERS, default="auto",
                              help="resemble: Resemble Enhance (slow, CPU; generative voice rebuild)")
    enhance_opts.add_argument("--atten", type=float, default=30.0, help="DeepFilterNet max suppression (dB)")
    rs = ResembleSettings()
    enhance_opts.add_argument("--tau", type=float, default=rs.tau,
                              help="Resemble temperature 0–1: lower stays closer to the input")
    enhance_opts.add_argument("--nfe", type=int, default=rs.nfe, help="Resemble steps 1–128: fewer = faster")
    enhance_opts.add_argument("--lambd", type=float, default=rs.lambd, help="Resemble denoise strength 0–1")
    enhance_opts.add_argument("--solver", choices=RESEMBLE_SOLVERS, default=rs.solver)
    enhance_opts.add_argument("--seed", type=int, default=rs.seed,
                              help="Resemble variation 0–9999: each gives a slightly different result")
    enhance_opts.add_argument("--target", type=float, default=-16.0, help="Target loudness (LUFS); -14 for social media")
    enhance_opts.add_argument("--format", choices=list(CODECS), default="m4a")
    enhance_opts.add_argument("--outputs", type=Path, help="Default: the current project's outputs/ folder")

    e = sub.add_parser("enhance", parents=[enhance_opts], help="Enhance audio files")
    e.add_argument("files", nargs="+")
    e.set_defaults(func=cmd_enhance)

    r = sub.add_parser("record", parents=[enhance_opts], help="Record lossless WAV from the microphone")
    r.add_argument("--device", help="Device name (or part of it) or index; default: built-in mic")
    r.add_argument("--duration", type=float, help="Seconds; without it, stop with Ctrl+C")
    r.add_argument("--name", help="Take name → <name>.wav (then <name>-2.wav …); default: rec_HHMMSS.wav")
    r.add_argument("--enhance", action="store_true", help="Enhance automatically after recording")
    r.add_argument("--list-devices", action="store_true", help="List audio input devices")
    r.add_argument("--records", type=Path, help="Default: the current project's voice-records/ folder")
    r.set_defaults(func=cmd_record)

    c = sub.add_parser("compare", help="Render stage-by-stage, loudness-matched A/B variants")
    c.add_argument("file")
    c.add_argument("--target", type=float, default=-16.0)
    c.add_argument("--outputs", type=Path)
    c.set_defaults(func=cmd_compare)

    pr = sub.add_parser("project", help="List projects / create, switch or rename one")
    pr.add_argument("action", nargs="?", choices=["list", "new", "use", "rename"], default="list")
    pr.add_argument("name", nargs="?")
    pr.add_argument("new_name", nargs="?", help="rename only: the new name")
    pr.set_defaults(func=cmd_project)

    g = sub.add_parser("gui", help="Open the recording / editing UI in the browser")
    g.add_argument("--port", type=int, default=8765)
    g.add_argument("--no-browser", action="store_true")
    g.set_defaults(func=cmd_gui)

    d = sub.add_parser("doctor", help="Check dependencies")
    d.set_defaults(func=cmd_doctor)

    args = p.parse_args(argv)
    return args.func(args)
