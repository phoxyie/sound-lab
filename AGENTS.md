# Sound Lab

Personal macOS tool for social-media voice content recorded on a MacBook's built-in mic: record,
clean up (DeepFilterNet3 + 2-pass loudnorm, default -16 LUFS / -1.5 dBTP), trim, speed up, join and
export. CLI (`soundlab`) plus a local web GUI. Usage docs: `README.md`; roadmap: `BACKLOG.md`.
Published as open source (MIT) at `github.com/phoxyie/sound-lab`; branch `windows-video` holds
unreviewed Windows + video (mp4/mov) support, not merged into `main`.

## Stack

- Python >= 3.10, setuptools (`pyproject.toml`); deps: Flask, sounddevice (CoreAudio). No torch in soundlab.
- ffmpeg (Homebrew) for all audio rendering; `deep-filter` binary in `tools/` (falls back to ffmpeg `afftdn`).
- Optional generative denoisers, each in its own venv under `tools/` with pinned versions and
  checksummed models: Resemble Enhance (Python 3.10/3.11), Sidon (Python 3.11+).
- Frontend: plain JS/HTML/CSS, no build step; wavesurfer.js 7.12.12 vendored (core, regions, timeline — same version).
- Tests: Playwright (WebKit, headless) — dev extra.

## Layout

- `soundlab/pipeline.py` — enhance pipeline (HPF → denoise → DSP preset → pre-gain + limiter → linear loudnorm); `_run_worker` runs the denoiser workers
- `soundlab/presets.py` — DSP chains (EQ, de-esser, compressor)
- `soundlab/resemble_worker.py`, `soundlab/sidon_worker.py` — run in their own venv's Python (subprocess, JSON progress lines on stdout, offline); never imported by soundlab
- `soundlab/editor.py` — trim (`atrim`) / speed (`atempo`) / join (`concat`) render + export
- `soundlab/projects.py` — projects; `soundlab/recorder.py` — mic recording via `sounddevice`
- `soundlab/compare.py` — stage-by-stage A/B variants; `soundlab/cli.py` — CLI entry (`soundlab.cli:main`)
- `soundlab/gui.py` — Flask API on 127.0.0.1 only; enhance/export are background jobs (`/api/jobs/<id>`, `/cancel`; `progress` callback 0–1 raises `Cancelled`)
- `soundlab/web/` — single-page frontend (`app.js`, `index.html`, `style.css`); `soundlab/web/vendor/` — wavesurfer.js
- `scripts/` — `install_deepfilter.sh`, `install_resemble.sh`, `install_sidon.sh`
- `tests/e2e_gui.py` — GUI end-to-end test
- Untracked: `tools/` (binaries, tool venvs, models), `projects/` (user data), `.venv/`
- Docs: `README.md` (usage, pipeline, license and credits), `LICENSE` (MIT); local only, removed from the
  public GitHub copy: `BACKLOG.md` (roadmap, phase checklists), `oxob-docs/` (oxob)

## Commands

```bash
pip install -e .                           # in .venv; installs `soundlab`
pip install -e ".[dev]" && playwright install webkit   # test deps, once
.venv/bin/soundlab doctor                  # dependency check (shows installed denoisers)
.venv/bin/soundlab gui                     # GUI at http://127.0.0.1:8765
.venv/bin/soundlab enhance FILE            # also: record, compare, project
.venv/bin/python tests/e2e_gui.py          # GUI e2e test (WebKit, headless; --headed to watch)
node --check soundlab/web/app.js           # JS syntax check
```

## Data

- `projects/<name>/{inputs,outputs,voice-records}`; current project in `projects/.current` (shared by CLI and GUI).
- Outputs: `outputs/YYYY-MM-DD/` with a `.json` report next to each file; reports keep absolute paths, re-based after a rename (`gui._report_clips`).
- GUI timeline lives in browser `localStorage`, keyed by project name, clip paths include the project folder — a rename must migrate both (GUI rename does).
- Timeline previews render to the system temp dir (`soundlab-preview/`), never into projects.

## Fixed constraints

- All UI text, messages, comments and docs are English.
- Edits are non-destructive: source files never change; trim/speed apply only on render.
- Deleting moves to the Trash (`SOUNDLAB_TRASH` overrides the folder), never `rm`.
- Defaults come from the user's ear tests and change only after an A/B (`soundlab compare`):
  preset `flat` with full DeepFilterNet; Resemble tau 0.5, nfe 32, lambd 1.0, midpoint, seed 0.
- The e2e test is muted (`?mute=1`), creates and deletes its own temporary project, uses
  `SOUNDLAB_TRASH` / `SOUNDLAB_REVEAL_CMD`, never uses the mic (`SOUNDLAB_RECORD_INPUT=tone:<Hz>`)
  or the user's projects / Trash, and restores `projects/.current`. `SOUNDLAB_RESEMBLE_CONFIRM_SEC=1`
  makes every Resemble job show its duration dialog (default 60 s).
- Tighten output keeps > 99.5% of the source's energy on real takes.
- Resemble and Sidon run on CPU only (Resemble crashes on MPS; Sidon's CPU export has the device baked in).

## Known pitfalls

- loudnorm silently drops from `linear` to `dynamic` when gain would exceed the TP target or LRA exceeds the target → pipeline pre-gains + limits and stretches LRA; check `normalization_type` in reports.
- Never capture the mic with ffmpeg `avfoundation`: it drops 512-sample blocks (~7%, clicks). Capture paths are tested by recording a played tone and checking for discontinuities.
- `atempo` length differs from `(end - start) / speed` by up to ~20 ms: fade-outs anchor to the real end (`areverse,afade,areverse`), never a computed `st=`.
- Continuous halves of a take render identically only because `editor.merge_continuous` joins them.
- Pause/silence detection uses 20 ms RMS windows (`pipeline.rms_windows`), not `silencedetect` (per-sample, broken by noise peaks). `_tight` reports keep `threshold_db` / `speech_db` and re-tightening reuses them. Slider full left (`pause_threshold`) cuts everything 18 dB under the voice, leaving 0.05 s gaps.
- Editor playhead is clamped to the trim range (`I`/`O`/arrows move only inside it).
- wavesurfer timeline plugin tick spacing stops at 1 s; `tuneRuler` sets ticks per zoom.
- Playwright WebKit: after a simulated HTML5 `drag_to` the next `pointerdown` is swallowed (click somewhere neutral first); wavesurfer ignores clicks ~10 ms after a drag (wait ~150 ms); expected 4xx responses log as console errors (the test filters those it triggers); the headless media clock sometimes ends playback early (wall-time checks retry an early end once).
- Resemble: denoise-only mode passes room noise through; seed changes the result about as much as `tau` (seed after model load, compare at the same seed); solver steps are staticmethods; `deepspeed` must be installed (imported at load); first progress line can take ~20 s, so `_resemble` polls for cancel.
- Sidon hears a 16 kHz copy (above 8 kHz is generated); feature extractor settings from `facebook/w2v-bert-2.0` kept in `tools/sidon-model`, loaded with `HF_HUB_OFFLINE`; torch 2.15 needs Python 3.11+.
