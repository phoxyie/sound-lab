# Sound Lab

Personal tool for social media voice content recorded on a MacBook mic: record, clean up
(DeepFilterNet3 + 2-pass loudnorm), trim, speed up, join and export. CLI + local web GUI.
Usage docs: `README.md` · roadmap: `BACKLOG.md`.

**If `HANDOFF.md` exists, read it first** — it's a one-off note from the previous session.
Delete it once its content is picked up (done, or moved into `BACKLOG.md`).

## Commands

```bash
.venv/bin/soundlab gui                     # GUI at http://127.0.0.1:8765
.venv/bin/soundlab enhance FILE            # CLI; also: record, compare, project, doctor
.venv/bin/python tests/e2e_gui.py          # GUI end-to-end test (Playwright/WebKit, headless)
node --check soundlab/web/app.js           # quick JS syntax check
```

`deep-filter` lives in `tools/` (`scripts/install_deepfilter.sh`); without it the pipeline falls back to ffmpeg `afftdn`.
Optional Resemble Enhance: own venv `tools/resemble-venv` + weights `tools/resemble-model`
(`scripts/install_resemble.sh`, pinned versions + checksums); `soundlab doctor` shows whether it's there.

## Layout

- `soundlab/pipeline.py` — enhance pipeline (HPF → denoise → DSP preset → gain+limiter → linear loudnorm)
- `soundlab/resemble_worker.py` — runs Resemble Enhance in its own venv's Python (subprocess, JSON progress
  lines on stdout); never imported by soundlab, which has no torch
- `soundlab/editor.py` — trim (`atrim`) / speed (`atempo`) / join (`concat`) render + export
- `soundlab/projects.py` — projects; `soundlab/recorder.py` — mic recording via CoreAudio (`sounddevice`)
- `soundlab/gui.py` — Flask API, bound to 127.0.0.1 only; `soundlab/web/` — single-page frontend
  (`app.js`, wavesurfer.js vendored in `web/vendor/`, no build step). Enhance / export are background
  jobs (`/api/jobs/<id>`, cancel via `/cancel`): the pipeline's `progress` callback reports 0–1 and raises
  `Cancelled` once the job is cancelled.
- Data: `projects/<name>/{inputs,outputs,voice-records}`, current project in `projects/.current`
  (shared by CLI and GUI). The GUI timeline lives in the browser's `localStorage`, keyed by project
  name, with clip paths that include the project folder — renaming must migrate both (GUI rename
  does). Outputs go to `outputs/YYYY-MM-DD/` with a `.json` report next to each file.
- Timeline previews are rendered to the system temp dir (`soundlab-preview/`), never into projects.

## Conventions

- Everything is **English**: UI text, user-facing messages (errors, CLI output), code comments,
  README and BACKLOG. (Chat with the user follows their language.)
- Edits are non-destructive: source files are never modified; trim/speed apply only on render.
- Deleting = moving to the Trash (`SOUNDLAB_TRASH` overrides the folder), never `rm`.
- Default enhancement is preset `flat` with full DeepFilterNet — chosen by the user's ear test;
  don't change defaults without an A/B (`soundlab compare`). Resemble's defaults (tau 0.5, nfe 32,
  lambd 1.0, midpoint, seed 0) come from an ear-tested settings grid too.

## Testing rules

- Verify GUI changes with the e2e test and look at its screenshots before reporting done.
- The test must stay **muted** (`?mute=1`), create and delete its **own temporary project**, use
  `SOUNDLAB_TRASH` / `SOUNDLAB_REVEAL_CMD`, and never record from the mic or touch the user's
  projects or real Trash. Recording runs on a generated real-time tone via
  `SOUNDLAB_RECORD_INPUT=tone:<Hz>` (same recorder code, only the source differs). It restores `projects/.current` at the end.
- Audio claims ("sounds the same", "no click") are checked by rendering and comparing samples, not
  assumed — e.g. two continuous halves of a take only render identically because
  `editor.merge_continuous` joins them.

## Known pitfalls

- loudnorm silently drops from `linear` to `dynamic` mode when gain would exceed the TP target or
  measured LRA exceeds the target → pipeline pre-gains + limits and stretches LRA; check
  `normalization_type` in reports.
- Playwright WebKit: after a simulated HTML5 `drag_to`, the next `pointerdown` is swallowed —
  click somewhere neutral first. wavesurfer ignores clicks for ~10 ms after a drag — wait ~150 ms.
- Editor playhead is clamped to the trim range, so `I`/`O` and the arrow keys can only move inside it;
  widen via the edges.
- wavesurfer.js is 7.12.12 (`web/vendor/`: core, regions, timeline — keep plugins on the same
  version). The timeline plugin's own tick spacing stops at 1 s; `tuneRuler` sets ticks per zoom.
- WebKit logs expected 4xx responses as console errors; the test filters only those it triggers.
- Headless WebKit's media clock sometimes jumps ahead: even a bare `<audio>` page ends a 4.9 s file
  after 1.4–4 s now and then (no app event involved). Wall-time playback checks retry an early end once.
- `atempo` output length differs from `(end - start) / speed` by up to ~20 ms: anchor fade-outs to the
  real end (`areverse,afade,areverse`), never to a computed `st=`.
- Never capture the mic with ffmpeg `avfoundation`: it drops 512-sample blocks (~7%, a click at each
  gap, no warning). `recorder.py` uses `sounddevice` and ffmpeg only converts the raw samples. Test a
  capture path by recording a played test tone and checking it for discontinuities.
- `silencedetect` checks single samples, so noise peaks break up pauses; pause / silence detection
  uses 20 ms RMS windows (`pipeline.rms_windows`) with a threshold between noise floor and speech.
  Removing silence raises fresh estimates (p10 / p90 shift), so a `_tight` file's report keeps
  `threshold_db` and `speech_db` and re-tightening reuses them — otherwise each pass cuts more.
  The slider sets the threshold too (`pause_threshold`): full left cuts everything 18 dB under the
  voice and leaves 0.05 s gaps (jump cuts — the user wants continuous speech, no breathing room). Check Tighten changes on the user's real takes: the output
  must keep > 99.5% of the source's energy (cutting a syllable costs several %); counting loud 20 ms
  windows is misleading because cuts shift the window grid.
- Resemble Enhance: full enhance crashes on MPS (abort after minutes) → CPU only. Its denoise-only mode
  passed the user's room noise through untouched. It's generative: the seed changes the result about as
  much as `tau` does, so seed after the model load (which consumes the RNG) to keep a seed reproducible,
  and compare settings at the same seed. Its solver steps are staticmethods (the progress hook wraps
  them as such). `deepspeed` must be installed although inference never uses it (imported at load).
  The first progress line can take ~20 s, so `_resemble` polls for cancel instead of blocking on stdout.
- Enhancement reports keep absolute paths; after a project rename they're re-based on where the
  output lives now (`gui._report_clips`).
